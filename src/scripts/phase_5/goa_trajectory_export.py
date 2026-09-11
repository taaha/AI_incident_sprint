"""Stable, explicitly lossy trajectory export for the public GoA run release."""

from __future__ import annotations

import csv
import json
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from typing import Any


SCHEMA_VERSION = "1.0"
RAW_OUTPUT_LIMIT = 24_000


def iso_timestamp(value: Any) -> str | None:
    """Return a UTC ISO timestamp for the release's ISO strings or epoch milliseconds."""
    if value is None or value == "":
        return None
    if isinstance(value, (int, float)):
        return datetime.fromtimestamp(value / 1000, tz=timezone.utc).isoformat().replace("+00:00", "Z")
    if isinstance(value, str):
        try:
            return datetime.fromisoformat(value.replace("Z", "+00:00")).astimezone(timezone.utc).isoformat().replace("+00:00", "Z")
        except ValueError:
            return None
    return None


def _epoch(value: Any) -> float | None:
    stamp = iso_timestamp(value)
    return datetime.fromisoformat(stamp.replace("Z", "+00:00")).timestamp() if stamp else None


def _text(block: dict[str, Any]) -> str:
    return str(block.get("text") or "")


def _is_raw(block: dict[str, Any]) -> bool:
    return str(block.get("title") or "").lower() == "raw stdout"


def _is_stream_fragment(block: dict[str, Any]) -> bool:
    text = _text(block).lstrip()
    return _is_raw(block) or text.startswith('{"type":') or text.startswith('{"type" :')


def parse_stream_json(text: str) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Extract provider events and tool-use events from newline-delimited stream JSON.

    A clipped final line is reported as malformed rather than guessed at.
    """
    turns: list[dict[str, Any]] = []
    tools: list[dict[str, Any]] = []
    malformed = False
    for line in text.splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            event = json.loads(line)
        except json.JSONDecodeError:
            malformed = True
            continue
        if not isinstance(event, dict):
            malformed = True
            continue
        if event.get("type") in {"message_start", "assistant", "message_delta", "message_stop"}:
            turns.append(event)
        # Anthropic stream events place tool input in content_block; Codex may emit item/tool_call.
        block = event.get("content_block")
        item = event.get("item")
        candidate = block if isinstance(block, dict) else item if isinstance(item, dict) else event
        if isinstance(candidate, dict) and candidate.get("type") in {"tool_use", "tool_call", "function_call"}:
            tools.append(candidate)
    return turns, tools, malformed


def _serializable_block(block: dict[str, Any]) -> dict[str, Any]:
    return {key: block.get(key) for key in ("block_id", "step_id", "role", "kind", "title", "text", "created_at", "updated_at")}


def _agent_model(run: dict[str, Any], agent_id: str) -> str | None:
    for config in run.get("config", {}).get("agents", []):
        if config.get("agent_id") == agent_id:
            return config.get("model")
    return None


def build_run_records(run: dict[str, Any]) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Build decision records and one index row per configured/exported agent."""
    run_id = str(run["run_id"])
    transcripts = run.get("transcripts") or {}
    agent_ids = sorted(set(transcripts) | {str(item.get("agent_id")) for item in run.get("config", {}).get("agents", []) if item.get("agent_id")})
    records: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    index: list[dict[str, Any]] = []
    for agent_id in agent_ids:
        blocks = transcripts.get(agent_id) or []
        prompts = [(position, block) for position, block in enumerate(blocks) if block.get("kind") == "prompt" and str(block.get("step_id") or "").strip()]
        seen: set[str] = set()
        duplicate = False
        for _, block in prompts:
            step_id = str(block["step_id"])
            duplicate |= step_id in seen
            seen.add(step_id)
        ceiling = len(blocks) == 500
        missing_timestamp = 0
        truncated_history = ceiling
        for decision_index, (position, prompt) in enumerate(prompts):
            step_id = str(prompt["step_id"])
            timestamp = iso_timestamp(prompt.get("created_at"))
            if timestamp is None:
                missing_timestamp += 1
            current_time = _epoch(prompt.get("created_at"))
            same_step = [block for block in blocks[position + 1:] if str(block.get("step_id") or "") == step_id]
            # Prior blocks are persisted observable state, deliberately not a claimed CLI replay.
            prior = [_serializable_block(block) for block in blocks[:position]]
            raw_parts = [_text(block) for block in same_step if _is_raw(block)]
            stream_parts = [_text(block) for block in same_step if _is_stream_fragment(block)]
            raw_stdout = "\n".join(raw_parts) or None
            provider_turns: list[dict[str, Any]] = []
            extracted_tools: list[dict[str, Any]] = []
            raw_malformed = False
            for fragment in stream_parts:
                turns, tools, bad = parse_stream_json(fragment)
                provider_turns.extend(turns)
                extracted_tools.extend(tools)
                if fragment in raw_parts:
                    raw_malformed |= bad
            raw_truncated = bool(raw_stdout and (len(raw_stdout) >= RAW_OUTPUT_LIMIT or "[truncated" in raw_stdout.lower() or "truncated]" in raw_stdout.lower()))
            output_blocks = [_serializable_block(block) for block in same_step if block.get("role") == "assistant" or block.get("kind") == "tool"]
            response_present = any(_text(block).strip() for block in same_step if block.get("role") == "assistant")
            record = {
                "schema_version": SCHEMA_VERSION, "run_id": run_id, "agent_id": agent_id,
                "decision_step_index": decision_index, "step_id": step_id, "timestamp": timestamp,
                "model": _agent_model(run, agent_id),
                "input_context": {"reconstruction_type": "observable_approximation", "current_prompt": _text(prompt), "prior_transcript_blocks": prior, "run_config": run.get("config", {}), "prior_environment_events": []},
                "model_output": {"conversation_blocks": output_blocks, "raw_stdout": raw_stdout, "provider_turns": provider_turns},
                "tool_calls": extracted_tools + [_serializable_block(block) for block in same_step if block.get("kind") == "tool"],
                "environment_events": [],
                "data_quality": {"prompt_present": bool(_text(prompt).strip()), "response_present": response_present,
                    "conversation_history_truncated": truncated_history, "raw_stdout_present": raw_stdout is not None,
                    "raw_stdout_truncated": raw_truncated, "raw_stdout_parseable": None if raw_stdout is None else not raw_malformed,
                    "timestamp_complete": timestamp is not None, "unassigned_environment_events_present": False},
            }
            # source transcript order precedes agent/local index in deterministic ties.
            records.append(((current_time is None, current_time if current_time is not None else float("inf"), position, agent_id, decision_index), record))
        flags = {"conversation_history_truncated": ceiling, "duplicate_step_ids": duplicate, "missing_timestamps": bool(missing_timestamp), "no_transcript": not blocks}
        index.append({"run_id": run_id, "agent_id": agent_id, "trajectory_file": f"{run_id}.jsonl", "step_count": len(prompts), **flags})
    records.sort(key=lambda item: item[0])
    return [record for _, record in records], index


def validate_record(record: dict[str, Any]) -> None:
    required = {"schema_version", "run_id", "agent_id", "decision_step_index", "step_id", "timestamp", "model", "input_context", "model_output", "tool_calls", "environment_events", "data_quality"}
    assert set(record) == required
    assert record["schema_version"] == SCHEMA_VERSION and record["step_id"] and record["decision_step_index"] >= 0
    assert record["input_context"]["reconstruction_type"] == "observable_approximation"


def write_release(source_dir: Path, output_dir: Path, report_path: Path, summary_path: Path) -> dict[str, int]:
    output_dir.mkdir(parents=True, exist_ok=True)
    all_index: list[dict[str, Any]] = []
    all_records: list[dict[str, Any]] = []
    runs = []
    for source in sorted(source_dir.glob("*.json")):
        run = json.loads(source.read_text(encoding="utf-8"))
        records, index = build_run_records(run)
        runs.append(run)
        all_records.extend(records); all_index.extend(index)
        with (output_dir / f"{run['run_id']}.jsonl").open("w", encoding="utf-8") as handle:
            for record in records:
                validate_record(record)
                handle.write(json.dumps(record, ensure_ascii=False, separators=(",", ":")) + "\n")
    fields = ["run_id", "agent_id", "trajectory_file", "step_count", "conversation_history_truncated", "duplicate_step_ids", "missing_timestamps", "no_transcript"]
    with (output_dir / "trajectory_index.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader(); writer.writerows(all_index)
    _write_report(report_path, runs, all_records, all_index, summary_path)
    return {"runs": len(runs), "records": len(all_records), "trajectories": len(all_index)}


def _write_report(path: Path, runs: list[dict[str, Any]], records: list[dict[str, Any]], index: list[dict[str, Any]], summary_path: Path) -> None:
    summaries = list(csv.DictReader(summary_path.open(encoding="utf-8")))
    models = sorted({record["model"] for record in records if record["model"]})
    conditions = sorted({row["condition"] for row in summaries})
    quality = Counter()
    for record in records:
        quality["truncated raw output"] += record["data_quality"]["raw_stdout_truncated"]
        quality["unparseable raw output"] += record["data_quality"]["raw_stdout_present"] and not record["data_quality"]["raw_stdout_parseable"]
        quality["missing timestamps"] += not record["data_quality"]["timestamp_complete"]
        quality["unassigned events"] += record["data_quality"]["unassigned_environment_events_present"]
        quality["complete records"] += all((record["data_quality"]["prompt_present"], record["data_quality"]["response_present"], record["data_quality"]["timestamp_complete"]))
    counts = {name: sum(len(run.get(name, [])) for run in runs) for name in ("offers", "purchases", "reviews", "bots", "games")}
    analyzed_agents = sum(int(row["agents"]) for row in summaries)
    truncated_trajectories = sum(bool(row["conversation_history_truncated"]) for row in index)
    text = f"""# Game of Agents release statistics

Generated from the local 47-export release by `scripts/build_goa_trajectories.py`. The analyzed corpus is identified by the authoritative `game-of-agents/paper/data/run_summary.csv`; exports outside it are still represented in the JSONL release.

## Corpus overview

| Measure | Value |
| --- | ---: |
| Exported runs | {len(runs)} |
| Analyzed runs | {len(summaries)} |
| Exported agent trajectories | {len(index)} |
| Analyzed agent trajectories | {analyzed_agents} |
| Outer decision steps | {len(records)} |
| Model families | {', '.join(models) or 'none persisted'} |
| Conditions | {len(conditions)} |
| Analyzed run-duration range (h) | {min(float(x['duration_h']) for x in summaries):g}–{max(float(x['duration_h']) for x in summaries):g} |
| Chats (authoritative analyzed summary) | {sum(int(x['n_chat']) for x in summaries)} |
| Offers (all exports) | {counts['offers']} |
| Purchases (all exports) | {counts['purchases']} |
| Reviews (all exports) | {counts['reviews']} |
| Bots (all exports) | {counts['bots']} |
| Games (all exports) | {counts['games']} |

## Data quality

| Measure | Count | Unit |
| --- | ---: | --- |
| Truncated conversation histories (500-block ceiling) | {truncated_trajectories} | trajectories |
| Truncated raw output | {quality['truncated raw output']} | records |
| Unparseable raw output | {quality['unparseable raw output']} | records |
| Complete prompt/response/timestamp records | {quality['complete records']} | records |
| Partial records | {len(records) - quality['complete records']} | records |
| Missing timestamps | {quality['missing timestamps']} | records |
| Unassigned environment-event flags | {quality['unassigned events']} | records |

## Reconstruction limits

Each JSONL line is one outer saved prompt cycle (`step_id`), not a provider turn, transcript block, or tool event. Context is an **observable approximation**: persisted prompt/transcript material and run configuration. The released public collections are not detailed enough to attach events safely, so environment-event fields remain empty; stored tool output is the evidence of what a model actually observed. This does not claim to replay hidden CLI/session context, system instructions, filesystem state, or omitted tool output. Raw stdout is capped in the release (normally 24,000 characters); malformed and truncated captures are retained and flagged.

Offer listings are subject to the known pagination limitation: the public offer collection may not contain every historical listing. The existing run summary is authoritative where its analyzed-corpus fields are available, notably condition, duration, and chat/offer/purchase counts.

## Glossary

| Term | Definition |
| --- | --- |
| Run | One complete multi-agent Game of Agents execution. |
| Agent | An LLM-controlled participant in a run. |
| Agent trajectory | One agent's persisted activity within one run. |
| Outer decision step | One stored `kind: prompt` with a nonempty `step_id`; the JSONL unit. |
| Provider turn | An internal provider-stream turn; several can occur in one outer step. |
| Event | A logged public/environment action or outcome. |
| Checkpoint | A point immediately before a selected decision step. |
| Audit branch | A disposable counterfactual continuation from a checkpoint. |
| Whistleblower call | The extra model call in an audit branch. |
| Observable reconstruction | Persisted, visible context only; not exact model-state replay. |
| Positive trajectory | A trajectory satisfying a predeclared problematic-behavior criterion. |
| Negative trajectory | An eligible trajectory not satisfying that criterion. |
| Extreme ladderer | An eligible trajectory meeting the three-part laddering criterion in the study plan. |
| False marketplace claim | A listing whose objective numeric claim is unsupported by available state. |
"""
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(text, encoding="utf-8")
