import json
import sys
import threading
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import src.scripts.phase_7.ollama_marketplace_sentinel_shared_state as sentinel  # noqa: E402
from src.scripts.phase_7.ollama_marketplace_sentinel_shared_state import (
    EXPERIMENT_NAME, SELECTED_CHECKPOINT_IDS, build_shared_state_request,
    freeze_manifest, load_sentinel_items, run_sentinel,
)
from src.scripts.phase_7.ollama_whistleblower_audit import AuditItem  # noqa: E402


ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data/whistleblower"
EXPORTS = ROOT / "game-of-agents/.goa_data/runs"


class _FakeLogger:
    def __init__(self): self.logged = []
    def start_parent(self, *_args): pass
    def log_checkpoint(self, result, request): self.logged.append((result, request))
    def close(self): pass


def _item(timestamp="2026-04-08T02:00:00+00:00"):
    return AuditItem({"checkpoint_id": "example", "pair_id": "example", "cohort": "sentinel_shared_state",
                      "run_id": "run_example", "agent_id": "agent-1", "decision_step_index": "7",
                      "step_id": "step-7", "timestamp": timestamp}, {}, {})


def test_v7_manifest_freezes_exactly_the_approved_five(tmp_path):
    rows = freeze_manifest(DATA_DIR, tmp_path / "manifest.csv")
    assert [row["checkpoint_id"] for row in rows] == list(SELECTED_CHECKPOINT_IDS)
    assert len({(row["run_id"], row["agent_id"]) for row in rows}) == 5
    assert all(row["human_issue_summary"] for row in rows)


def test_shared_builder_keeps_pre_checkpoint_shared_surfaces_only(tmp_path):
    exports = tmp_path / "exports"; exports.mkdir()
    exported = {"comments": {"one": {"author_agent_id": "agent-2", "text": "shared chat", "created_at": "2026-04-08T01:00:00Z"},
                              "late": {"author_agent_id": "agent-3", "text": "future", "created_at": "2026-04-08T03:00:00Z"}},
                "offers": {"offer": {"seller_agent_id": "agent-1", "title": "offer", "created_at": "2026-04-08T01:10:00Z"}},
                "purchases": {"purchase": {"buyer_agent_id": "agent-2", "created_at": "2026-04-08T01:20:00Z"}},
                "reviews": {"review": {"author_agent_id": "agent-3", "created_at": "2026-04-08T01:30:00Z"}}}
    (exports / "run_example.json").write_text(json.dumps(exported), encoding="utf-8")
    request, summary = build_shared_state_request(_item(), exports)
    context = json.loads(request["messages"][1]["content"])
    assert [row["author_agent_id"] for row in context["shared_chat_messages"]] == ["agent-2"]
    assert len(context["shared_marketplace_offers"]) == len(context["shared_purchases"]) == len(context["shared_reviews"]) == 1
    assert context["public_leaderboard_snapshots"] == []
    assert not ({"prior_transcript_blocks", "current_prompt", "run_config", "tool_calls", "model_output", "labels", "taxonomy", "human_issue_summary"} & set(context))
    assert summary["future_events_included"] is False and summary["local_transcript_included"] is False


def test_v7_mock_run_is_five_way_and_resumes_completed_only(tmp_path, monkeypatch):
    manifest, results, rendered = tmp_path / "manifest.csv", tmp_path / "results.jsonl", tmp_path / "rendered"
    items = [AuditItem({"checkpoint_id": checkpoint_id, "pair_id": checkpoint_id, "cohort": "sentinel_shared_state",
                       "run_id": "run_example", "agent_id": f"agent-{index}", "decision_step_index": "7",
                       "step_id": f"step-{index}", "timestamp": "2026-04-08T02:00:00+00:00"}, {}, {})
             for index, checkpoint_id in enumerate(SELECTED_CHECKPOINT_IDS, 1)]
    monkeypatch.setattr(sentinel, "load_sentinel_items", lambda *_args: items)
    monkeypatch.setattr(sentinel, "build_shared_state_request", lambda item, *_args: (
        {"model": "mock", "stream": False, "messages": [{"role": "system", "content": "sentinel"}, {"role": "user", "content": json.dumps({"target_agent_id": item.checkpoint["agent_id"]})}]},
        {"chat_messages": 0, "offers": 0, "purchases": 0, "reviews": 0, "leaderboard_snapshots": 0,
         "local_transcript_included": False, "current_prompt_included": False, "run_config_included": False,
         "selected_step_output_included": False, "future_events_included": False},
    ))
    active = maximum = calls = 0
    lock = threading.Lock()

    failed_once = False

    def client(request, api_key, timeout, retries):
        nonlocal active, maximum, calls, failed_once
        assert api_key == "test-key"
        assert "prior_transcript_blocks" not in request["messages"][1]["content"]
        with lock:
            calls += 1; active += 1; maximum = max(maximum, active)
        time.sleep(0.05)
        with lock: active -= 1
        if not failed_once:
            failed_once = True
            raise RuntimeError("temporary provider failure")
        return "NO_REPORT", {"provider": "mock"}

    logger = _FakeLogger()
    first = run_sentinel(DATA_DIR, results, manifest_path=manifest, run_exports_dir=EXPORTS, rendered_dir=rendered,
                         api_key="test-key", client=client, mlflow_logger=logger)
    assert first == {"validated": 5, "skipped": 0, "written": 5}
    assert calls == 5 and maximum == 5 and len(logger.logged) == 5
    rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()]
    assert {row["checkpoint_id"] for row in rows} == set(SELECTED_CHECKPOINT_IDS)
    assert sum(row["status"] == "completed" for row in rows) == 4
    assert sum(row["status"] == "request_failed" for row in rows) == 1
    assert all((rendered / f"{identifier}.json").is_file() for identifier in SELECTED_CHECKPOINT_IDS)
    second = run_sentinel(DATA_DIR, results, manifest_path=manifest, run_exports_dir=EXPORTS, rendered_dir=rendered,
                          api_key="test-key", client=client, mlflow_logger=_FakeLogger())
    assert second == {"validated": 5, "skipped": 4, "written": 1}
    assert calls == 6
    third = run_sentinel(DATA_DIR, results, manifest_path=manifest, run_exports_dir=EXPORTS, rendered_dir=rendered,
                         api_key="test-key", client=client, mlflow_logger=_FakeLogger())
    assert third == {"validated": 5, "skipped": 5, "written": 0}
    assert EXPERIMENT_NAME == "phase7_ollama_marketplace_sentinel_shared_state_v7"
