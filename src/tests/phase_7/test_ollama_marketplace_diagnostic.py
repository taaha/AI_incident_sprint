import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.scripts.phase_7.ollama_marketplace_diagnostic import (  # noqa: E402
    EXPERIMENT_NAME,
    MLflowLogger,
    AuditItem,
    build_marketplace_request,
    load_marketplace_diagnostic_items,
    run_marketplace_diagnostic,
)


def test_fixed_cases_are_three_distinct_runs_with_visible_evidence():
    root = Path(__file__).resolve().parents[3]
    items = load_marketplace_diagnostic_items(root / "data/whistleblower")
    assert len(items) == 3
    assert len({item.checkpoint["run_id"] for item in items}) == 3
    assert {item.checkpoint["cohort"] for item in items} == {"marketplace_diagnostic"}


def test_request_is_label_blind_and_excludes_environment_events():
    root = Path(__file__).resolve().parents[3]
    item = load_marketplace_diagnostic_items(root / "data/whistleblower")[0]
    request = build_marketplace_request(item)
    context = json.loads(request["messages"][1]["content"])
    assert "marketplace_diagnostic" not in request["messages"][1]["content"]
    assert "environment_events" not in context
    assert "prior_environment_events" not in context
    assert context["visible_evidence_snippets"]
    assert set(context) == {"current_prompt", "visible_evidence_snippets", "phase5_data_quality"}


def test_free_form_response_and_prompt_version_are_preserved(tmp_path, monkeypatch):
    item = AuditItem(
        {"checkpoint_id": "marketplace_checkpoint_001", "pair_id": "marketplace_case_001", "cohort": "marketplace_diagnostic", "run_id": "run", "agent_id": "agent", "model": "model", "decision_step_index": "0", "step_id": "step", "timestamp": "now"},
        {"evidence_term": "evidence"}, {"input_context": {}, "model_output": {"text": "evidence"}, "tool_calls": [], "data_quality": {}},
    )
    monkeypatch.setattr("src.scripts.phase_7.ollama_marketplace_diagnostic.load_marketplace_diagnostic_items", lambda data_dir: [item])
    results = tmp_path / "results.jsonl"

    def client(request, key, timeout, retries):
        return "Visible rank contradicts the marketplace title.", {"done": True}

    summary = run_marketplace_diagnostic(tmp_path, results, api_key="test-key", prompt_version="test-v2",
                                         instructions="custom prompt", client=client,
                                         mlflow_logger=MLflowLogger("unused", "unused", enabled=False))
    row = json.loads(results.read_text(encoding="utf-8"))
    assert summary == {"validated": 1, "skipped": 0, "written": 1}
    assert row["raw_response"] == "Visible rank contradicts the marketplace title."
    assert row["request_config"]["prompt_version"] == "test-v2"
    assert row["response_validation"]["performed"] is False


def test_marketplace_diagnostic_uses_its_own_experiment():
    assert EXPERIMENT_NAME == "phase7_ollama_marketplace_diagnostic_v2_evidence"
