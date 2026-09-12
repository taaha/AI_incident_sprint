"""Phase 7 V6: sanitized terminal-checkpoint marketplace discovery cohort.

This is deliberately separate from the historical V3--V5 diagnostics.  It
freezes a 30-trajectory manifest before provider calls and sends only the
selected agent's non-prompt history available before its final decision.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import random
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable

try:
    from src.scripts.phase_7.ollama_marketplace_pre_action_audit import PRE_ACTION_INSTRUCTIONS
    from src.scripts.phase_7.ollama_marketplace_pre_action_sanitized import (
        build_sanitized_request,
    )
    from src.scripts.phase_7.ollama_whistleblower_audit import (
        DEFAULT_MODEL, OLLAMA_CHAT_URL, RESULT_SCHEMA_VERSION, AuditItem, MLflowLogger,
        OllamaAuthenticationError, ValidationError, _acquire_run_lock, _append_result,
        _existing_results, _required, load_dotenv_key, ollama_chat,
    )
except ModuleNotFoundError as error:
    if error.name != "src":
        raise
    from ollama_marketplace_pre_action_audit import PRE_ACTION_INSTRUCTIONS
    from ollama_marketplace_pre_action_sanitized import build_sanitized_request
    from ollama_whistleblower_audit import (
        DEFAULT_MODEL, OLLAMA_CHAT_URL, RESULT_SCHEMA_VERSION, AuditItem, MLflowLogger,
        OllamaAuthenticationError, ValidationError, _acquire_run_lock, _append_result,
        _existing_results, _required, load_dotenv_key, ollama_chat,
    )


PROMPT_VERSION = "phase7-marketplace-terminal-v6-discovery-sanitized"
EXPERIMENT_NAME = "phase7_ollama_marketplace_terminal_v6_discovery_sanitized"
SAMPLING_SEED = 20260912
TARGET_COUNT = 30
HISTORICAL_DIAGNOSTICS = {
    ("run_vskkr3r3ov8jeq", "agent-1"),
    ("run_oa7r0sc1pd876i", "claude-3"),
    ("run_zaeqhzla1udfcf", "agent-2"),
    ("run_7w9i4y0spafbbj", "agent-5"),
    ("run_hcgt8jiaqpv1r5", "gpt-1"),
    ("run_hcgt8jiaqpv1r5", "gpt-3"),
}
REUSE_EXCEPTIONS = {
    ("run_vskkr3r3ov8jeq", "agent-1"),
    ("run_7w9i4y0spafbbj", "agent-5"),
    ("run_hcgt8jiaqpv1r5", "gpt-1"),
    ("run_hcgt8jiaqpv1r5", "gpt-3"),
    ("run_zaeqhzla1udfcf", "agent-2"),
}
MANIFEST_FIELDS = (
    "run_id", "agent_id", "model", "condition", "step_count", "trajectory_file",
    "selected_decision_step_index", "step_id", "timestamp", "selection_order",
    "sampling_seed", "reuse_exception",
)


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file():
        raise ValidationError(f"missing required artifact: {path}")
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def _excluded_condition(condition: str) -> bool:
    lowered = condition.lower()
    return any(name in lowered for name in ("competitive", "adversarial", "bad actor"))


def _load_records(data_dir: Path, row: dict[str, str], records_cache: dict[str, list[dict[str, Any]]] | None = None) -> list[dict[str, Any]]:
    file_name = row["trajectory_file"]
    if Path(file_name).name != file_name:
        raise ValidationError(f"unsafe trajectory filename: {file_name}")
    path = data_dir / file_name
    if not path.is_file():
        raise ValidationError(f"missing trajectory file: {path}")
    cache_key = str(path)
    if records_cache is not None and cache_key in records_cache:
        all_records = records_cache[cache_key]
    else:
        all_records = []
        with path.open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    all_records.append(json.loads(line))
                except json.JSONDecodeError as error:
                    raise ValidationError(f"malformed Phase 5 JSONL in {path}:{line_number}") from error
        if records_cache is not None:
            records_cache[cache_key] = all_records
    records = [record for record in all_records
               if record.get("run_id") == row["run_id"] and record.get("agent_id") == row["agent_id"]]
    records.sort(key=lambda record: int(record["decision_step_index"]))
    if len(records) != int(row["step_count"]):
        raise ValidationError(f"trajectory index disagrees with persisted records for {(row['run_id'], row['agent_id'])}")
    expected = list(range(len(records)))
    actual = [int(record["decision_step_index"]) for record in records]
    if actual != expected:
        raise ValidationError(f"trajectory has non-contiguous decision-step indexes for {(row['run_id'], row['agent_id'])}")
    return records


def _manifest_candidate(data_dir: Path, trajectory: dict[str, str], condition: str, order: int,
                        records_cache: dict[str, list[dict[str, Any]]]) -> dict[str, str]:
    records = _load_records(data_dir, trajectory, records_cache)
    record = records[-1]
    _required(record, ("decision_step_index", "step_id", "timestamp", "model", "input_context", "data_quality"), "Phase 5 record")
    if int(record["decision_step_index"]) != int(trajectory["step_count"]) - 1:
        raise ValidationError("terminal record is not step_count - 1")
    key = (trajectory["run_id"], trajectory["agent_id"])
    return {
        "run_id": trajectory["run_id"], "agent_id": trajectory["agent_id"], "model": str(record["model"]),
        "condition": condition, "step_count": trajectory["step_count"], "trajectory_file": trajectory["trajectory_file"],
        "selected_decision_step_index": str(record["decision_step_index"]), "step_id": str(record["step_id"]),
        "timestamp": str(record["timestamp"]), "selection_order": str(order), "sampling_seed": str(SAMPLING_SEED),
        "reuse_exception": str(key in REUSE_EXCEPTIONS).lower(),
    }


def build_manifest_rows(data_dir: Path, run_summary_path: Path | None = None) -> list[dict[str, str]]:
    """Select the frozen V6 cohort, maximizing run diversity in its order."""
    index = _read_csv(data_dir / "trajectory_index.csv")
    run_summary_path = run_summary_path or data_dir.parent.parent / "game-of-agents/paper/data/run_summary.csv"
    summaries = {row["run_id"]: row for row in _read_csv(run_summary_path)}
    candidates_by_run: dict[str, list[dict[str, str]]] = {}
    exceptions_found: set[tuple[str, str]] = set()
    for trajectory in index:
        _required(trajectory, ("run_id", "agent_id", "trajectory_file", "step_count"), "trajectory index")
        summary = summaries.get(trajectory["run_id"])
        if summary is None:
            continue
        key = (trajectory["run_id"], trajectory["agent_id"])
        standard = (
            int(trajectory["step_count"]) > 15
            and int(summary["n_offers"]) > 0
            and not _excluded_condition(summary["condition"])
            and key not in HISTORICAL_DIAGNOSTICS
        )
        exception = key in REUSE_EXCEPTIONS
        if not standard and not exception:
            continue
        if int(trajectory["step_count"]) <= 15:
            raise ValidationError(f"selected trajectory fails >15 step threshold: {key}")
        if exception:
            exceptions_found.add(key)
        candidates_by_run.setdefault(trajectory["run_id"], []).append(trajectory)
    if exceptions_found != REUSE_EXCEPTIONS:
        raise ValidationError("one or more approved historical reuse exceptions are missing")
    candidates = [trajectory for group in candidates_by_run.values() for trajectory in group]
    if len(candidates) != TARGET_COUNT:
        raise ValidationError(f"V6 eligibility produced {len(candidates)} trajectories; expected exactly {TARGET_COUNT}")

    rng = random.Random(SAMPLING_SEED)
    run_ids = sorted(candidates_by_run)
    rng.shuffle(run_ids)
    for run_id in run_ids:
        candidates_by_run[run_id].sort(key=lambda row: row["agent_id"])
        rng.shuffle(candidates_by_run[run_id])
    ordered: list[dict[str, str]] = []
    while any(candidates_by_run.values()):
        for run_id in run_ids:
            if candidates_by_run[run_id]:
                ordered.append(candidates_by_run[run_id].pop(0))
    records_cache: dict[str, list[dict[str, Any]]] = {}
    rows = [_manifest_candidate(data_dir, trajectory, summaries[trajectory["run_id"]]["condition"], order, records_cache)
            for order, trajectory in enumerate(ordered, start=1)]
    _validate_manifest_rows(rows)
    return rows


def _validate_manifest_rows(rows: list[dict[str, str]]) -> None:
    if len(rows) != TARGET_COUNT:
        raise ValidationError(f"manifest has {len(rows)} rows; expected {TARGET_COUNT}")
    keys = [(row["run_id"], row["agent_id"]) for row in rows]
    if len(set(keys)) != TARGET_COUNT:
        raise ValidationError("manifest has duplicate run/agent identities")
    if any(int(row["step_count"]) <= 15 for row in rows):
        raise ValidationError("manifest contains a trajectory with 15 or fewer steps")
    if {key for key in keys if key in HISTORICAL_DIAGNOSTICS} != REUSE_EXCEPTIONS:
        raise ValidationError("manifest has unauthorized historical diagnostic reuse")
    if [int(row["selection_order"]) for row in rows] != list(range(1, TARGET_COUNT + 1)):
        raise ValidationError("manifest selection order is not contiguous")
    if any(row["sampling_seed"] != str(SAMPLING_SEED) for row in rows):
        raise ValidationError("manifest sampling seed is inconsistent")


def freeze_manifest(data_dir: Path, manifest_path: Path, run_summary_path: Path | None = None) -> list[dict[str, str]]:
    """Write the deterministic manifest once, or reject a changed existing manifest."""
    expected = build_manifest_rows(data_dir, run_summary_path)
    if manifest_path.exists():
        actual = _read_csv(manifest_path)
        if actual != expected:
            raise ValidationError(f"existing frozen manifest differs from deterministic V6 selection: {manifest_path}")
        return actual
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS)
        writer.writeheader()
        writer.writerows(expected)
    return expected


def load_terminal_items(data_dir: Path, manifest_path: Path, run_summary_path: Path | None = None) -> list[AuditItem]:
    rows = freeze_manifest(data_dir, manifest_path, run_summary_path)
    _validate_manifest_rows(rows)
    items: list[AuditItem] = []
    records_cache: dict[str, list[dict[str, Any]]] = {}
    for row in rows:
        records = _load_records(data_dir, row, records_cache)
        record = records[-1]
        if str(record["decision_step_index"]) != row["selected_decision_step_index"]:
            raise ValidationError("manifest terminal checkpoint disagrees with persisted record")
        checkpoint = {
            "checkpoint_id": f"terminal_v6_{int(row['selection_order']):03d}",
            "pair_id": f"terminal_v6_case_{int(row['selection_order']):03d}", "cohort": "terminal_discovery",
            "checkpoint_name": "final", "run_id": row["run_id"], "agent_id": row["agent_id"], "model": row["model"],
            "decision_step_index": row["selected_decision_step_index"], "step_id": row["step_id"], "timestamp": row["timestamp"],
        }
        items.append(AuditItem(checkpoint, {"trajectory_file": row["trajectory_file"]}, record))
    return items


def build_terminal_request(item: AuditItem, model: str = DEFAULT_MODEL) -> tuple[dict[str, Any], dict[str, Any]]:
    """Reuse V5's sanitizer with the frozen V4 integrity-monitor instruction."""
    request, summary = build_sanitized_request(item, model, PRE_ACTION_INSTRUCTIONS)
    context = json.loads(request["messages"][1]["content"])
    context["context_policy"] = "selected_agent_prior_history_without_original_task_instructions"
    request["messages"][1]["content"] = json.dumps(context, ensure_ascii=False, separators=(",", ":"))
    _validate_terminal_request(request, summary)
    return request, summary


def _validate_terminal_request(request: dict[str, Any], summary: dict[str, Any]) -> None:
    context = json.loads(request["messages"][1]["content"])
    forbidden = {"current_prompt", "run_config", "model_output", "tool_calls", "environment_events", "labels", "condition", "taxonomy"}
    if forbidden & set(context):
        raise ValidationError("terminal request includes forbidden context fields")
    if any(block.get("kind") == "prompt" for block in context["prior_transcript_blocks"]):
        raise ValidationError("terminal request retained a prompt block")
    if any(summary[name] for name in ("run_config_included", "current_prompt_included", "selected_step_output_included")):
        raise ValidationError("terminal sanitation summary is invalid")


def _result(item: AuditItem, model: str, timeout_seconds: float, retries: int, raw_response: str | None,
            provider_metadata: dict[str, Any], elapsed_seconds: float, status: str, error: str | None,
            sanitation_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION, "checkpoint_id": item.checkpoint["checkpoint_id"],
        "checkpoint_name": "final", "pair_id": item.checkpoint["pair_id"], "cohort": "terminal_discovery",
        "run_id": item.checkpoint["run_id"], "agent_id": item.checkpoint["agent_id"],
        "decision_step_index": int(item.checkpoint["decision_step_index"]), "step_id": item.checkpoint["step_id"],
        "timestamp": item.checkpoint["timestamp"],
        "request_config": {"provider": "ollama_cloud", "url": OLLAMA_CHAT_URL, "model": model,
                           "prompt_version": PROMPT_VERSION, "stream": False, "timeout_seconds": timeout_seconds,
                           "retries": retries, "context_boundary": "before_selected_outer_decision_step",
                           "context_policy": "selected_agent_prior_history_without_original_task_instructions"},
        "sanitation_summary": sanitation_summary, "raw_response": raw_response,
        "response_validation": {"performed": False, "reason": "free_form_terminal_discovery"},
        "parse_result": {"valid": raw_response is not None, "kind": "free_form_diagnostic"},
        "provider_metadata": provider_metadata, "elapsed_seconds": elapsed_seconds,
        "context_quality": item.record["data_quality"], "status": status, "error": error,
    }


def write_discovery_report(results_path: Path, report_path: Path) -> None:
    """Write a compact, non-adjudicative index of raw discovery responses."""
    results = _existing_results(results_path)
    lines = ["# Phase 7 V6 Terminal Discovery Results", "", "Raw responses are discovery artifacts, not labels or adjudications.", "",
             "| Run / agent | Steps | Final checkpoint | Status | Raw response preview |", "|---|---:|---:|---|---|"]
    for result in results.values():
        raw = (result.get("raw_response") or "").replace("\n", " ").strip()
        preview = raw[:220] + ("…" if len(raw) > 220 else "")
        lines.append(f"| {result['run_id']} / {result['agent_id']} | {result['decision_step_index'] + 1} | {result['decision_step_index']} | {result['status']} | {preview.replace('|', '\\|')} |")
    lines.extend(["", f"Raw result records: `{results_path}`", f"MLflow experiment: `{EXPERIMENT_NAME}`", ""])
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text("\n".join(lines), encoding="utf-8")


def _completed_checkpoint_ids(path: Path | None) -> set[str]:
    """Return only successful prior calls; failures are eligible for a retry batch."""
    if path is None:
        return set()
    return {checkpoint_id for checkpoint_id, result in _existing_results(path).items()
            if result.get("status") == "completed"}


def run_terminal_discovery(data_dir: Path, results_path: Path, *, manifest_path: Path,
                           report_path: Path | None = None, run_summary_path: Path | None = None,
                           model: str = DEFAULT_MODEL, api_key: str | None = None, timeout_seconds: float = 300,
                           retries: int = 2, force: bool = False, validate_only: bool = False,
                           parallelism: int = 1, skip_completed_from: Path | None = None,
                           client: Callable[[dict[str, Any], str, float, int], tuple[str, dict[str, Any]]] = ollama_chat,
                           mlflow_logger: MLflowLogger | None = None) -> dict[str, int]:
    if parallelism < 1:
        raise ValidationError("parallelism must be at least one")
    all_items = load_terminal_items(data_dir, manifest_path, run_summary_path)
    # Materialize and validate the full frozen cohort before filtering it for a resume batch.
    all_requests = {item.checkpoint["checkpoint_id"]: build_terminal_request(item, model) for item in all_items}
    already_completed = _completed_checkpoint_ids(skip_completed_from)
    items = [item for item in all_items if item.checkpoint["checkpoint_id"] not in already_completed]
    requests = {item.checkpoint["checkpoint_id"]: all_requests[item.checkpoint["checkpoint_id"]] for item in items}
    if validate_only:
        return {"validated": len(all_items), "skipped": len(already_completed), "written": 0}
    if not api_key:
        raise RuntimeError("OLLAMA_API_KEY is required for live terminal discovery execution")
    lock_path = _acquire_run_lock(results_path)
    try:
        existing = _existing_results(results_path)
        tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", f"sqlite:///{(data_dir / 'mlflow.db').resolve()}")
        logger = mlflow_logger or MLflowLogger(tracking_uri, EXPERIMENT_NAME)
        logger.start_parent("phase7-marketplace-terminal-discovery", {"model": model, "prompt_version": PROMPT_VERSION,
                                                                         "checkpoint_count": str(len(items)), "sampling_seed": str(SAMPLING_SEED),
                                                                         "parallelism": str(parallelism), "resume_completed_skipped": str(len(already_completed))})
        written = skipped = 0
        try:
            pending: list[tuple[AuditItem, dict[str, Any], dict[str, Any]]] = []
            for item in items:
                checkpoint_id = item.checkpoint["checkpoint_id"]
                if checkpoint_id in existing and not force:
                    skipped += 1
                    continue
                if checkpoint_id in existing:
                    raise ValidationError(f"cannot force checkpoint {checkpoint_id} into an existing result file; choose a new results path")
                request, summary = requests[checkpoint_id]
                pending.append((item, request, summary))

            def call_one(item: AuditItem, request: dict[str, Any], summary: dict[str, Any]) -> tuple[dict[str, Any], dict[str, Any]]:
                started = time.monotonic(); raw_response: str | None = None; provider_metadata: dict[str, Any] = {}
                try:
                    raw_response, provider_metadata = client(request, api_key, timeout_seconds, retries)
                    status, error = "completed", None
                except Exception as error_value:
                    status, error = "request_failed", str(error_value)
                result = _result(item, model, timeout_seconds, retries, raw_response, provider_metadata, time.monotonic() - started, status, error, summary)
                return result, request

            # Provider work is concurrent; JSONL writes and MLflow mutations remain serialized.
            with ThreadPoolExecutor(max_workers=parallelism, thread_name_prefix="phase7-ollama") as executor:
                futures = [executor.submit(call_one, item, request, summary) for item, request, summary in pending]
                for future in as_completed(futures):
                    result, request = future.result()
                    _append_result(results_path, result)
                    logger.log_checkpoint(result, request)
                    written += 1
        finally:
            logger.close()
        if report_path is not None:
            write_discovery_report(results_path, report_path)
        return {"validated": len(items), "skipped": skipped, "written": written}
    finally:
        lock_path.unlink(missing_ok=True)


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description="Run the Phase 7 V6 sanitized terminal discovery cohort.")
    parser.add_argument("--data-dir", type=Path, default=root / "data/whistleblower")
    parser.add_argument("--manifest", type=Path, default=root / "data/whistleblower/phase7_terminal_discovery_manifest.csv")
    parser.add_argument("--results", type=Path, default=root / "data/whistleblower/audit_marketplace_terminal_v6_discovery_sanitized.jsonl")
    parser.add_argument("--report", type=Path, default=root / "data/whistleblower/phase7_terminal_discovery_report.md")
    parser.add_argument("--run-summary", type=Path, default=root / "game-of-agents/paper/data/run_summary.csv")
    parser.add_argument("--model", default=DEFAULT_MODEL); parser.add_argument("--timeout", type=float, default=300)
    parser.add_argument("--retries", type=int, default=2); parser.add_argument("--validate-only", action="store_true")
    parser.add_argument("--force", action="store_true")
    parser.add_argument("--parallelism", type=int, default=1)
    parser.add_argument("--skip-completed-from", type=Path, default=None,
                        help="prior V6 results file whose completed checkpoints should not be rerun")
    args = parser.parse_args(); key = None if args.validate_only else load_dotenv_key(root / ".env")
    print(json.dumps(run_terminal_discovery(args.data_dir, args.results, manifest_path=args.manifest, report_path=args.report,
                                             run_summary_path=args.run_summary, model=args.model, api_key=key,
                                             timeout_seconds=args.timeout, retries=args.retries, force=args.force,
                                             validate_only=args.validate_only, parallelism=args.parallelism,
                                             skip_completed_from=args.skip_completed_from), sort_keys=True))


if __name__ == "__main__":
    main()
