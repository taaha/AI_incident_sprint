import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import src.scripts.phase_7.ollama_marketplace_v10_matched as v10  # noqa: E402
from src.scripts.phase_7.ollama_whistleblower_audit import AuditItem, ValidationError  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data/whistleblower"
EXPORTS = ROOT / "game-of-agents/.goa_data/runs"


def test_v10_frozen_cohort_has_25_ordered_triplets():
    items = v10.load_v10_items(DATA, DATA / v10.V9_MANIFEST_NAME)
    assert len(items) == 75
    for start in range(0, len(items), 3):
        triplet = items[start:start + 3]
        assert [item.checkpoint["condition"] for item in triplet] == [
            "whistleblower_local", "whistleblower_local", "sentinel_shared_state"]
        assert [item.checkpoint["checkpoint_name"] for item in triplet] == ["p50", "p100", "p100"]
        assert len({item.checkpoint["pair_id"] for item in triplet}) == 1
        assert triplet[1].checkpoint["timestamp"] == triplet[2].checkpoint["timestamp"]
        assert int(triplet[1].checkpoint["decision_step_index"]) == int(triplet[2].checkpoint["decision_step_index"])


def test_v10_prompt_policy_and_schema_are_identical_after_role():
    local = v10._prompt(v10.LOCAL_ROLE)
    sentinel = v10._prompt(v10.SENTINEL_ROLE)
    assert local.removeprefix(v10.LOCAL_ROLE) == sentinel.removeprefix(v10.SENTINEL_ROLE)
    assert v10.SHARED_DECISION_POLICY in local
    assert v10.OUTPUT_SCHEMA_INSTRUCTIONS in sentinel


def test_v10_local_is_sanitized_and_sentinel_is_final_public_state_only(tmp_path):
    local_item = AuditItem({"checkpoint_id": "local", "agent_id": "agent-1"}, {}, {
        "input_context": {"current_prompt": "secret", "run_config": {"secret": True},
                          "prior_transcript_blocks": [{"kind": "prompt", "text": "secret"}, {"kind": "text", "text": "agent-2 observed"}]},
        "data_quality": {"ok": True},
    })
    request, _ = v10.build_local_request(local_item, "mock")
    local_context = json.loads(request["messages"][1]["content"])
    assert local_context["prior_transcript_blocks"] == [{"kind": "text", "text": "agent-2 observed"}]
    assert "current_prompt" not in local_context and "run_config" not in local_context

    run_id = "run_final"
    (tmp_path / f"{run_id}.json").write_text(json.dumps({
        "comments": {"one": {"author_agent_id": "agent-1", "text": "public"}},
        "offers": {"one": {"offer_id": "offer-1", "title": "public"}}, "purchases": {}, "reviews": {},
        "agents": {"agent-1": {"best_elo": 10, "workspace": "/private", "runtime": "private"}},
        "final_scores": {"agent-1": 10}, "payouts": {"agent-1": 1}, "transcripts": {"agent-1": "private"}, "config": {"private": True},
    }), encoding="utf-8")
    sentinel_item = AuditItem({"checkpoint_id": "sentinel", "agent_id": "agent-1", "run_id": run_id}, {}, {})
    request, summary = v10.build_sentinel_request(sentinel_item, tmp_path, "mock")
    sentinel_context = json.loads(request["messages"][1]["content"])
    serialized = json.dumps(sentinel_context)
    assert "transcripts" not in sentinel_context and "config" not in sentinel_context
    assert "workspace" not in serialized and "runtime" not in serialized
    assert "recovered_review_command_activity" not in sentinel_context
    assert summary["private_command_logs_included"] is False


def test_v10_parser_and_default_sequential_contract():
    yes = json.dumps({"report": True, "category": "claim", "target_agent": "agent-1", "summary": "Detailed supported explanation.", "evidence": ["fact"], "severity": 1, "confidence": 0.8})
    no = json.dumps({"report": False, "category": "none", "target_agent": "agent-1", "summary": "Detailed absence explanation.", "evidence": [], "severity": 0, "confidence": 0.8})
    assert v10.parse_structured_response(yes, {"agent-1"})["report"] is True
    assert v10.parse_structured_response(no, {"agent-1"})["report"] is False
    with pytest.raises(ValidationError):
        v10.run_v10(DATA, manifest_path=DATA / v10.V9_MANIFEST_NAME, results_path=Path("unused"),
                    rendered_dir=Path("unused"), run_exports_dir=EXPORTS, parallelism=2, validate_only=True)


def test_v10_renders_before_calls_and_retries_only_latest_failure(tmp_path, monkeypatch):
    items = [AuditItem({"checkpoint_id": f"case-{number}", "pair_id": f"pair-{number // 3}", "run_id": "run",
                        "agent_id": "agent-1", "condition": "whistleblower_local", "checkpoint_name": "p50",
                        "decision_step_index": "0", "step_id": "step", "timestamp": "2026-01-01T00:00:00Z",
                        "source_v6_checkpoint_id": "terminal"}, {}, {}) for number in range(75)]
    monkeypatch.setattr(v10, "load_v10_items", lambda *_args: items)
    monkeypatch.setattr(v10, "build_local_request", lambda item, model: (
        {"model": model, "stream": False, "messages": [{"role": "system", "content": "x"}, {"role": "user", "content": json.dumps({"context_boundary": "before_selected_outer_decision_step", "target_agent_id": "agent-1"})}]},
        {"run_config_included": False, "current_prompt_included": False, "selected_step_output_included": False},
    ))

    class Logger:
        def start_parent(self, *_args): pass
        def log_checkpoint(self, *_args): pass
        def close(self): pass

    calls = []
    def client(request, *_args):
        assert len(list((tmp_path / "rendered").glob("*.json"))) == 75
        calls.append(request)
        if len(calls) == 1:
            raise RuntimeError("temporary")
        return json.dumps({"report": False, "category": "none", "target_agent": "agent-1", "summary": "No concrete evidence.", "evidence": [], "severity": 0, "confidence": 0.8}), {"mock": True}

    kwargs = dict(manifest_path=tmp_path / "manifest.csv", results_path=tmp_path / "results.jsonl",
                  rendered_dir=tmp_path / "rendered", run_exports_dir=tmp_path, api_key="key", client=client,
                  mlflow_logger=Logger())
    first = v10.run_v10(DATA, **kwargs)
    assert first == {"validated": 75, "skipped": 0, "written": 75}
    second = v10.run_v10(DATA, **kwargs)
    assert second == {"validated": 75, "skipped": 74, "written": 1}
    rows = [json.loads(line) for line in (tmp_path / "results.jsonl").read_text().splitlines()]
    assert len(rows) == 76 and rows[-1]["checkpoint_id"] == "case-0" and rows[-1]["attempt"] == 2
