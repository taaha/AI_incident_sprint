import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.scripts.phase_7.ollama_positive_diagnostic import (  # noqa: E402
    EXPECTED_TARGET_CHECKPOINTS,
    EXPERIMENT_NAME,
    MLflowLogger,
    AuditItem,
    build_diagnostic_request,
    load_positive_diagnostic_items,
    run_positive_diagnostic,
)


def test_targeted_selection_uses_original_positive_agents_at_75_percent():
    root = Path(__file__).resolve().parents[3]
    items = load_positive_diagnostic_items(root / "data/whistleblower")
    trajectories = {(item.checkpoint["run_id"], item.checkpoint["agent_id"]) for item in items}
    assert len(items) == len(trajectories) == EXPECTED_TARGET_CHECKPOINTS
    assert {item.checkpoint["target_progress"] for item in items} == {"0.75"}
    assert {item.checkpoint["cohort"] for item in items} == {"positive"}


def test_diagnostic_request_is_label_blind_and_only_uses_observable_context():
    root = Path(__file__).resolve().parents[3]
    item = load_positive_diagnostic_items(root / "data/whistleblower")[0]
    request = build_diagnostic_request(item)
    prompt = request["messages"][0]["content"]
    context = json.loads(request["messages"][1]["content"])
    assert "positive" not in prompt.lower()
    assert "extreme-laddering" not in request["messages"][1]["content"]
    assert "environment_events" not in context
    assert "prior_environment_events" not in context
    assert set(context) == {"current_prompt", "prior_transcript_blocks", "run_config", "visible_model_output", "visible_tool_data", "phase5_data_quality"}


def test_free_form_responses_are_preserved_and_resume_without_duplicates(tmp_path, monkeypatch):
    item = AuditItem(
        {"checkpoint_id": "diagnostic_run_agent_1", "pair_id": "diagnostic_run", "cohort": "positive", "run_id": "run", "agent_id": "agent", "model": "model", "decision_step_index": "0", "step_id": "step", "timestamp": "now"},
        {"model": "model"},
        {"input_context": {}, "model_output": {}, "tool_calls": [], "data_quality": {}},
    )
    monkeypatch.setattr("src.scripts.phase_7.ollama_positive_diagnostic.load_positive_diagnostic_items", lambda data_dir: [item])
    results = tmp_path / "positive_diagnostic.jsonl"
    response = "The context is truncated and does not show a concrete misleading claim."

    def client(request, key, timeout, retries):
        return response, {"done": True}

    summary = run_positive_diagnostic(tmp_path, results, api_key="test-key", client=client,
                                      mlflow_logger=MLflowLogger("unused", "unused", enabled=False))
    rows = [json.loads(line) for line in results.open(encoding="utf-8")]
    assert summary == {"validated": 1, "skipped": 0, "written": 1}
    assert len(rows) == len({row["checkpoint_id"] for row in rows}) == 1
    assert {row["raw_response"] for row in rows} == {response}
    assert {row["response_validation"]["performed"] for row in rows} == {False}
    assert {row["status"] for row in rows} == {"completed"}
    resumed = run_positive_diagnostic(tmp_path, results, api_key="test-key", client=client,
                                      mlflow_logger=MLflowLogger("unused", "unused", enabled=False))
    assert resumed == {"validated": 1, "skipped": 1, "written": 0}


def test_provider_failure_is_recorded_without_leaking_credential(tmp_path, monkeypatch):
    item = AuditItem(
        {"checkpoint_id": "diagnostic_run_agent_1", "pair_id": "diagnostic_run", "cohort": "positive", "run_id": "run", "agent_id": "agent", "model": "model", "decision_step_index": "0", "step_id": "step", "timestamp": "now"},
        {"model": "model"},
        {"input_context": {}, "model_output": {}, "tool_calls": [], "data_quality": {}},
    )
    monkeypatch.setattr("src.scripts.phase_7.ollama_positive_diagnostic.load_positive_diagnostic_items", lambda data_dir: [item])
    results = tmp_path / "positive_diagnostic.jsonl"

    def failure(request, key, timeout, retries):
        raise RuntimeError("provider unavailable")

    run_positive_diagnostic(tmp_path, results, api_key="secret-key", client=failure,
                            mlflow_logger=MLflowLogger("unused", "unused", enabled=False))
    written = results.read_text(encoding="utf-8")
    assert "secret-key" not in written
    assert {json.loads(line)["status"] for line in written.splitlines()} == {"request_failed"}


def test_diagnostic_uses_a_distinct_mlflow_experiment_name():
    assert EXPERIMENT_NAME == "phase7_ollama_whistleblower_diagnostic_targeted"
