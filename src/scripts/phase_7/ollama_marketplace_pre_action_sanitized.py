"""Sanitized Phase 7 pre-action marketplace-integrity leakage control.

This condition keeps only the selected agent's non-prompt history.  It omits
the environment-authored prompt/configuration surfaces that could reveal the
experimental objective, while retaining agent-generated text and observations.
"""

from __future__ import annotations

import argparse
import csv
import json
import os
import time
from pathlib import Path
from typing import Any, Callable

try:
    from src.scripts.phase_7.ollama_marketplace_pre_action_audit import (
        CHECKPOINTS, PRE_ACTION_INSTRUCTIONS, _checkpoint_indexes,
    )
    from src.scripts.phase_7.ollama_whistleblower_audit import (
        DEFAULT_MODEL, OLLAMA_CHAT_URL, RESULT_SCHEMA_VERSION, AuditItem, MLflowLogger,
        OllamaAuthenticationError, ValidationError, _acquire_run_lock, _append_result,
        _existing_results, _required, load_dotenv_key, ollama_chat,
    )
except ModuleNotFoundError as error:
    if error.name != "src":
        raise
    from ollama_marketplace_pre_action_audit import CHECKPOINTS, PRE_ACTION_INSTRUCTIONS, _checkpoint_indexes
    from ollama_whistleblower_audit import (
        DEFAULT_MODEL, OLLAMA_CHAT_URL, RESULT_SCHEMA_VERSION, AuditItem, MLflowLogger,
        OllamaAuthenticationError, ValidationError, _acquire_run_lock, _append_result,
        _existing_results, _required, load_dotenv_key, ollama_chat,
    )


PROMPT_VERSION = "phase7-marketplace-preaction-v5-sanitized-task-instructions"
EXPERIMENT_NAME = "phase7_ollama_marketplace_preaction_v5_sanitized_task_instructions"
TARGETS = (
    ("positive", "run_vskkr3r3ov8jeq", "agent-1"),
    ("positive", "run_oa7r0sc1pd876i", "claude-3"),
    ("positive", "run_zaeqhzla1udfcf", "agent-2"),
    # These are comparison cases selected from low taxonomy scores, not validated negatives.
    ("comparison_low_taxonomy", "run_7w9i4y0spafbbj", "agent-5"),
    ("comparison_low_taxonomy", "run_hcgt8jiaqpv1r5", "gpt-1"),
    ("comparison_low_taxonomy", "run_hcgt8jiaqpv1r5", "gpt-3"),
)


def load_sanitized_items(data_dir: Path) -> list[AuditItem]:
    """Materialize 0/50/100% pre-action contexts for the six fixed cases."""
    index_path = data_dir / "trajectory_index.csv"
    if not index_path.is_file():
        raise ValidationError(f"missing required artifact: {index_path}")
    with index_path.open(newline="", encoding="utf-8") as handle:
        index = {(row["run_id"], row["agent_id"]): row for row in csv.DictReader(handle)}
    items: list[AuditItem] = []
    for target_ordinal, (cohort, run_id, agent_id) in enumerate(TARGETS, start=1):
        trajectory = index.get((run_id, agent_id))
        if trajectory is None:
            raise ValidationError(f"missing trajectory index row for {(run_id, agent_id)}")
        _required(trajectory, ("trajectory_file", "step_count"), "trajectory index")
        file_name = trajectory["trajectory_file"]
        if Path(file_name).name != file_name:
            raise ValidationError(f"unsafe trajectory filename: {file_name}")
        records: list[dict[str, Any]] = []
        with (data_dir / file_name).open(encoding="utf-8") as handle:
            for line_number, line in enumerate(handle, start=1):
                try:
                    record = json.loads(line)
                except json.JSONDecodeError as error:
                    raise ValidationError(f"malformed Phase 5 JSONL in {file_name}:{line_number}") from error
                if record.get("run_id") == run_id and record.get("agent_id") == agent_id:
                    records.append(record)
        records.sort(key=lambda record: int(record["decision_step_index"]))
        if len(records) != int(trajectory["step_count"]):
            raise ValidationError(f"trajectory index disagrees with persisted records for {(run_id, agent_id)}")
        for checkpoint_ordinal, (checkpoint_name, record_index) in enumerate(_checkpoint_indexes(len(records)), start=1):
            record = records[record_index]
            _required(record, ("decision_step_index", "step_id", "timestamp", "model", "input_context", "data_quality"), "Phase 5 record")
            checkpoint = {
                "checkpoint_id": f"sanitized_{target_ordinal:03d}_{checkpoint_ordinal:03d}",
                "pair_id": f"sanitized_case_{target_ordinal:03d}", "cohort": cohort,
                "checkpoint_name": checkpoint_name, "run_id": run_id, "agent_id": agent_id,
                "model": record["model"], "decision_step_index": str(record["decision_step_index"]),
                "step_id": record["step_id"], "timestamp": record["timestamp"],
            }
            items.append(AuditItem(checkpoint, {"trajectory_file": file_name}, record))
    return items


def sanitize_prior_blocks(blocks: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Remove only environment prompt blocks; preserve every other prior block verbatim."""
    retained = [block for block in blocks if block.get("kind") != "prompt"]
    summary = {
        "prior_block_count_before": len(blocks),
        "prompt_blocks_removed": len(blocks) - len(retained),
        "prior_block_count_after": len(retained),
        "run_config_included": False,
        "current_prompt_included": False,
        "selected_step_output_included": False,
    }
    return retained, summary


def build_sanitized_request(item: AuditItem, model: str = DEFAULT_MODEL,
                            instructions: str = PRE_ACTION_INSTRUCTIONS) -> tuple[dict[str, Any], dict[str, Any]]:
    context = item.record.get("input_context") or {}
    prior = context.get("prior_transcript_blocks") or []
    if not isinstance(prior, list) or not all(isinstance(block, dict) for block in prior):
        raise ValidationError("prior_transcript_blocks must be a list of objects")
    sanitized_prior, summary = sanitize_prior_blocks(prior)
    observable_context = {
        "context_boundary": "before_selected_outer_decision_step",
        "context_policy": "selected_agent_prior_history_without_original_task_instructions",
        "prior_transcript_blocks": sanitized_prior,
        "phase5_data_quality": item.record.get("data_quality"),
    }
    request = {"model": model, "stream": False, "messages": [
        {"role": "system", "content": instructions},
        {"role": "user", "content": json.dumps(observable_context, ensure_ascii=False, separators=(",", ":"))},
    ]}
    _validate_sanitized_request(request, summary)
    return request, summary


def _validate_sanitized_request(request: dict[str, Any], summary: dict[str, Any]) -> None:
    """Assert the request itself, not merely the source record, is leakage-free."""
    context = json.loads(request["messages"][1]["content"])
    forbidden_keys = {"run_config", "current_prompt", "model_output", "tool_calls", "environment_events", "labels"}
    if forbidden_keys & set(context):
        raise ValidationError("sanitized request includes forbidden context fields")
    prior = context["prior_transcript_blocks"]
    if any(block.get("kind") == "prompt" for block in prior):
        raise ValidationError("sanitized request retained a prompt block")
    if summary["run_config_included"] or summary["current_prompt_included"] or summary["selected_step_output_included"]:
        raise ValidationError("invalid sanitation summary")


def _result(item: AuditItem, model: str, timeout_seconds: float, retries: int, raw_response: str | None,
            provider_metadata: dict[str, Any], elapsed_seconds: float, status: str, error: str | None,
            sanitation_summary: dict[str, Any]) -> dict[str, Any]:
    return {
        "schema_version": RESULT_SCHEMA_VERSION, "checkpoint_id": item.checkpoint["checkpoint_id"],
        "checkpoint_name": item.checkpoint["checkpoint_name"], "pair_id": item.checkpoint["pair_id"],
        "cohort": item.checkpoint["cohort"], "run_id": item.checkpoint["run_id"], "agent_id": item.checkpoint["agent_id"],
        "decision_step_index": int(item.checkpoint["decision_step_index"]), "step_id": item.checkpoint["step_id"],
        "timestamp": item.checkpoint["timestamp"],
        "request_config": {"provider": "ollama_cloud", "url": OLLAMA_CHAT_URL, "model": model,
                           "prompt_version": PROMPT_VERSION, "stream": False, "timeout_seconds": timeout_seconds, "retries": retries,
                           "context_boundary": "before_selected_outer_decision_step",
                           "context_policy": "selected_agent_prior_history_without_original_task_instructions"},
        "sanitation_summary": sanitation_summary, "raw_response": raw_response,
        "response_validation": {"performed": False, "reason": "free_form_pre_action_sanitized_condition"},
        "parse_result": {"valid": raw_response is not None, "kind": "free_form_diagnostic"},
        "provider_metadata": provider_metadata, "elapsed_seconds": elapsed_seconds,
        "context_quality": item.record["data_quality"], "status": status, "error": error,
    }


def run_sanitized_audit(data_dir: Path, results_path: Path, *, model: str = DEFAULT_MODEL,
                        api_key: str | None = None, timeout_seconds: float = 300, retries: int = 2,
                        force: bool = False, validate_only: bool = False,
                        client: Callable[[dict[str, Any], str, float, int], tuple[str, dict[str, Any]]] = ollama_chat,
                        mlflow_logger: MLflowLogger | None = None) -> dict[str, int]:
    items = load_sanitized_items(data_dir)
    # Materialize every request before either a provider call or a result-file lock.
    requests = {item.checkpoint["checkpoint_id"]: build_sanitized_request(item, model) for item in items}
    if validate_only:
        return {"validated": len(items), "skipped": 0, "written": 0}
    if not api_key:
        raise RuntimeError("OLLAMA_API_KEY is required for live sanitized audit execution")
    lock_path = _acquire_run_lock(results_path)
    try:
        existing = _existing_results(results_path)
        tracking_uri = os.environ.get("MLFLOW_TRACKING_URI", f"sqlite:///{(data_dir / 'mlflow.db').resolve()}")
        logger = mlflow_logger or MLflowLogger(tracking_uri, EXPERIMENT_NAME)
        logger.start_parent("phase7-marketplace-preaction-sanitized", {"model": model, "prompt_version": PROMPT_VERSION,
                                                                         "checkpoint_count": str(len(items)), "dataset_schema": "1.0"})
        written = skipped = 0
        try:
            for item in items:
                checkpoint_id = item.checkpoint["checkpoint_id"]
                if checkpoint_id in existing and not force:
                    skipped += 1
                    continue
                if checkpoint_id in existing:
                    raise ValidationError(f"cannot force checkpoint {checkpoint_id} into an existing result file; choose a new results path")
                request, summary = requests[checkpoint_id]
                started = time.monotonic(); raw_response: str | None = None; provider_metadata: dict[str, Any] = {}
                try:
                    raw_response, provider_metadata = client(request, api_key, timeout_seconds, retries)
                    status, error = "completed", None
                except OllamaAuthenticationError as error_value:
                    result = _result(item, model, timeout_seconds, retries, raw_response, provider_metadata, time.monotonic() - started, "request_failed", str(error_value), summary)
                    _append_result(results_path, result); logger.log_checkpoint(result, request); raise
                except Exception as error_value:
                    status, error = "request_failed", str(error_value)
                result = _result(item, model, timeout_seconds, retries, raw_response, provider_metadata, time.monotonic() - started, status, error, summary)
                _append_result(results_path, result); logger.log_checkpoint(result, request); written += 1
        finally:
            logger.close()
        return {"validated": len(items), "skipped": skipped, "written": written}
    finally:
        lock_path.unlink(missing_ok=True)


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description="Run the Phase 7 sanitized marketplace pre-action audit.")
    parser.add_argument("--data-dir", type=Path, default=root / "data/whistleblower")
    parser.add_argument("--results", type=Path, default=root / "data/whistleblower/audit_marketplace_preaction_v5_sanitized_task_instructions.jsonl")
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--timeout", type=float, default=300); parser.add_argument("--retries", type=int, default=2)
    parser.add_argument("--validate-only", action="store_true"); parser.add_argument("--force", action="store_true")
    args = parser.parse_args(); key = None if args.validate_only else load_dotenv_key(root / ".env")
    print(json.dumps(run_sanitized_audit(args.data_dir, args.results, model=args.model, api_key=key,
                                         timeout_seconds=args.timeout, retries=args.retries, force=args.force,
                                         validate_only=args.validate_only), sort_keys=True))


if __name__ == "__main__":
    main()
