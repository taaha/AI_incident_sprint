import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.scripts.phase_7.ollama_whistleblower_audit import (
    AuditItem,
    MLflowLogger,
    ValidationError,
    build_request,
    load_audit_items,
    parse_response,
    run_audit,
)


def record(run="run", agent="agent", index=0, step="step", timestamp="2026-01-01T00:00:00Z"):
    return {
        "schema_version": "1.0", "run_id": run, "agent_id": agent, "decision_step_index": index,
        "step_id": step, "timestamp": timestamp, "model": "original-model",
        "input_context": {"current_prompt": "persisted prompt", "prior_transcript_blocks": [{"text": "old"}], "run_config": {"x": 1}, "prior_environment_events": []},
        "model_output": {"conversation_blocks": [{"text": "visible"}]}, "tool_calls": [{"name": "tool"}],
        "environment_events": [{"must_not": "be sent"}], "data_quality": {"prompt_present": True},
    }


def write_cohort(tmp_path, *, bad_timestamp=False):
    trajectory = "run.jsonl"
    (tmp_path / trajectory).write_text(json.dumps(record()) + "\n", encoding="utf-8")
    label_fields = ["schema_version", "run_id", "agent_id", "trajectory_file", "cohort", "model"]
    checkpoint_fields = ["schema_version", "checkpoint_id", "pair_id", "cohort", "run_id", "agent_id", "model", "decision_step_index", "step_id", "timestamp"]
    with (tmp_path / "trajectory_labels.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=label_fields); writer.writeheader()
        writer.writerow({"schema_version": "1.0", "run_id": "run", "agent_id": "agent", "trajectory_file": trajectory, "cohort": "positive", "model": "original-model"})
    with (tmp_path / "checkpoint_index.csv").open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=checkpoint_fields); writer.writeheader()
        writer.writerow({"schema_version": "1.0", "checkpoint_id": "checkpoint_001", "pair_id": "pair_001", "cohort": "positive", "run_id": "run", "agent_id": "agent", "model": "original-model", "decision_step_index": "0", "step_id": "step", "timestamp": "wrong" if bad_timestamp else "2026-01-01T00:00:00Z"})


def test_request_is_only_persisted_observable_context(tmp_path):
    write_cohort(tmp_path)
    item = load_audit_items(tmp_path)[0]
    request = build_request(item, "glm-5.3-flash")
    assert request["model"] == "glm-5.3-flash" and request["stream"] is False
    context = json.loads(request["messages"][1]["content"])
    assert context["current_prompt"] == "persisted prompt"
    assert "environment_events" not in context
    assert "prior_environment_events" not in context


def test_response_contract_accepts_no_report_and_valid_report_only():
    assert parse_response("NO_REPORT")["kind"] == "no_report"
    valid = '{"report":true,"category":"incentive","target_agent":"agent-1","evidence":["visible block"],"severity":2,"confidence":0.8}'
    assert parse_response(valid)["kind"] == "report"
    assert not parse_response(" NO_REPORT")["valid"]
    assert not parse_response('{"report":false}')["valid"]


def test_rejects_manifest_record_identity_disagreement_before_calls(tmp_path):
    write_cohort(tmp_path, bad_timestamp=True)
    with pytest.raises(ValidationError, match="disagrees"):
        load_audit_items(tmp_path)


def test_accepts_phase6_negative_checkpoint_alias(tmp_path):
    write_cohort(tmp_path)
    label_path = tmp_path / "trajectory_labels.csv"
    label_rows = list(csv.DictReader(label_path.open()))
    label_rows[0]["cohort"] = "negative_candidate"
    with label_path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=label_rows[0].keys()); writer.writeheader(); writer.writerows(label_rows)
    path = tmp_path / "checkpoint_index.csv"
    rows = list(csv.DictReader(path.open()))
    rows[0]["cohort"] = "negative"
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys()); writer.writeheader(); writer.writerows(rows)
    assert load_audit_items(tmp_path)[0].checkpoint["cohort"] == "negative"


def test_provider_error_and_malformed_response_are_terminal_and_append_safe(tmp_path):
    write_cohort(tmp_path)
    results = tmp_path / "results.jsonl"
    logger = MLflowLogger("unused", "unused", enabled=False)
    def malformed(request, key, timeout, retries):
        return "almost NO_REPORT", {"done": True}
    summary = run_audit(tmp_path, results, api_key="key", client=malformed, mlflow_logger=logger)
    assert summary == {"validated": 1, "skipped": 0, "written": 1}
    result = json.loads(results.read_text())
    assert result["status"] == "invalid_output" and result["raw_response"] == "almost NO_REPORT"
    assert result["request_config"]["model"] == "glm-5.3-flash"
    assert result["context_quality"] == {"prompt_present": True}
    assert run_audit(tmp_path, results, api_key="key", client=malformed, mlflow_logger=logger)["skipped"] == 1


def test_provider_failure_is_recorded_without_leaking_key(tmp_path):
    write_cohort(tmp_path)
    results = tmp_path / "results.jsonl"
    def failure(request, key, timeout, retries):
        raise RuntimeError("provider unavailable")
    run_audit(tmp_path, results, api_key="secret-key", client=failure, mlflow_logger=MLflowLogger("unused", "unused", enabled=False))
    result = json.loads(results.read_text())
    assert result["status"] == "request_failed"
    assert "secret-key" not in results.read_text()


def test_validate_only_never_needs_credentials_or_network(tmp_path):
    write_cohort(tmp_path)
    assert run_audit(tmp_path, tmp_path / "results.jsonl", validate_only=True) == {"validated": 1, "skipped": 0, "written": 0}


def test_actual_sanity_manifest_writes_18_terminal_records_with_mocked_provider(tmp_path):
    root = Path(__file__).resolve().parents[3]
    results = tmp_path / "audit_results.jsonl"
    calls = []
    def no_report(request, key, timeout, retries):
        calls.append(request["model"])
        return "NO_REPORT", {"done": True}
    summary = run_audit(
        root / "data/whistleblower", results, api_key="test-key", client=no_report,
        mlflow_logger=MLflowLogger("unused", "unused", enabled=False),
    )
    rows = [json.loads(line) for line in results.open()]
    assert summary == {"validated": 18, "skipped": 0, "written": 18}
    assert len(rows) == len({row["checkpoint_id"] for row in rows}) == len(calls) == 18
    assert {row["status"] for row in rows} == {"completed"}
    assert run_audit(root / "data/whistleblower", results, api_key="test-key", client=no_report,
                     mlflow_logger=MLflowLogger("unused", "unused", enabled=False))["skipped"] == 18


def test_mlflow_parent_and_nested_metadata_with_mock(monkeypatch):
    events = []
    class Run:
        def __enter__(self): return self
        def __exit__(self, *args): return None
    class FakeMLflow:
        def set_tracking_uri(self, value): events.append(("uri", value))
        def set_experiment(self, value): events.append(("experiment", value))
        def start_run(self, **kwargs): events.append(("start", kwargs)); return Run()
        def log_params(self, value): events.append(("params", value))
        def log_metrics(self, value): events.append(("metrics", value))
        def log_dict(self, value, name): events.append(("dict", name))
        def end_run(self): events.append(("end",))
    monkeypatch.setitem(sys.modules, "mlflow", FakeMLflow())
    logger = MLflowLogger("file:///tmp/mlruns", "phase7")
    logger.start_parent("parent", {"model": "glm-5.3-flash"})
    result = {"checkpoint_id": "checkpoint_001", "pair_id": "pair", "cohort": "positive", "request_config": {"model": "glm-5.3-flash"}, "elapsed_seconds": 0.2, "parse_result": {"valid": True, "kind": "report"}}
    logger.log_checkpoint(result, {"model": "glm-5.3-flash"})
    logger.close()
    assert ("start", {"run_name": "checkpoint_001", "nested": True}) in events
    assert ("dict", "request.json") in events and ("dict", "result.json") in events
