import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.scripts.phase_7.ollama_marketplace_pre_action_audit import (  # noqa: E402
    EXPERIMENT_NAME,
    MLflowLogger,
    AuditItem,
    build_pre_action_request,
    load_pre_action_items,
    run_pre_action_audit,
)


def test_selects_three_distinct_trajectories_at_zero_half_and_final_steps():
    root = Path(__file__).resolve().parents[3]
    items = load_pre_action_items(root / "data/whistleblower")
    assert len(items) == 9
    assert len({(item.checkpoint["run_id"], item.checkpoint["agent_id"]) for item in items}) == 3
    for run_id, agent_id in {(item.checkpoint["run_id"], item.checkpoint["agent_id"]) for item in items}:
        selected = [item for item in items if (item.checkpoint["run_id"], item.checkpoint["agent_id"]) == (run_id, agent_id)]
        assert [item.checkpoint["checkpoint_name"] for item in selected] == ["zero", "half", "final"]


def test_request_excludes_selected_step_output_and_tool_data():
    item = AuditItem(
        {"checkpoint_id": "preaction_001_001", "checkpoint_name": "zero"}, {},
        {"input_context": {"current_prompt": "before", "prior_transcript_blocks": [{"text": "earlier marketplace offer"}], "run_config": {"run": 1}},
         "model_output": {"must_not": "appear"}, "tool_calls": [{"must_not": "appear"}], "data_quality": {"prompt_present": True}},
    )
    request = build_pre_action_request(item)
    context = json.loads(request["messages"][1]["content"])
    assert context["context_boundary"] == "before_selected_outer_decision_step"
    assert context["context_policy"] == "full_selected_agent_prior_transcript_only"
    assert context["prior_transcript_blocks"] == [{"text": "earlier marketplace offer"}]
    assert "model_output" not in context and "tool_calls" not in context
    assert "must_not" not in request["messages"][1]["content"]


def test_free_form_pre_action_results_resume_without_duplicates(tmp_path, monkeypatch):
    item = AuditItem(
        {"checkpoint_id": "preaction_001_001", "checkpoint_name": "zero", "pair_id": "case", "cohort": "marketplace_pre_action", "run_id": "run", "agent_id": "agent", "model": "model", "decision_step_index": "0", "step_id": "step", "timestamp": "now"},
        {}, {"input_context": {}, "data_quality": {}},
    )
    monkeypatch.setattr("src.scripts.phase_7.ollama_marketplace_pre_action_audit.load_pre_action_items", lambda data_dir: [item])
    results = tmp_path / "results.jsonl"

    def client(request, key, timeout, retries):
        return "No prior evidence is visible.", {"done": True}

    summary = run_pre_action_audit(tmp_path, results, api_key="test-key", client=client,
                                   mlflow_logger=MLflowLogger("unused", "unused", enabled=False))
    assert summary == {"validated": 1, "skipped": 0, "written": 1}
    row = json.loads(results.read_text(encoding="utf-8"))
    assert row["request_config"]["context_boundary"] == "before_selected_outer_decision_step"
    assert row["request_config"]["context_policy"] == "full_selected_agent_prior_transcript_only"
    assert run_pre_action_audit(tmp_path, results, api_key="test-key", client=client,
                                mlflow_logger=MLflowLogger("unused", "unused", enabled=False)) == {"validated": 1, "skipped": 1, "written": 0}


def test_pre_action_audit_has_own_mlflow_experiment():
    assert EXPERIMENT_NAME == "phase7_ollama_marketplace_preaction_v4_sensitive_integrity"
