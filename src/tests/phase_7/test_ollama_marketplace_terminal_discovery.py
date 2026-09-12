import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.scripts.phase_7.ollama_marketplace_terminal_discovery import (  # noqa: E402
    EXPERIMENT_NAME,
    HISTORICAL_DIAGNOSTICS,
    REUSE_EXCEPTIONS,
    SAMPLING_SEED,
    TARGET_COUNT,
    build_manifest_rows,
    build_terminal_request,
    load_terminal_items,
    run_terminal_discovery,
)
from src.scripts.phase_7.ollama_whistleblower_audit import AuditItem, ValidationError  # noqa: E402


ROOT = Path(__file__).resolve().parents[3]
DATA_DIR = ROOT / "data/whistleblower"
RUN_SUMMARY = ROOT / "game-of-agents/paper/data/run_summary.csv"


def test_v6_manifest_is_30_strict_step_trajectories_with_only_approved_reuse():
    rows = build_manifest_rows(DATA_DIR, RUN_SUMMARY)
    assert len(rows) == TARGET_COUNT
    assert len({(row["run_id"], row["agent_id"]) for row in rows}) == TARGET_COUNT
    assert all(int(row["step_count"]) > 15 for row in rows)
    assert all(int(row["selected_decision_step_index"]) == int(row["step_count"]) - 1 for row in rows)
    historical = {(row["run_id"], row["agent_id"]) for row in rows} & HISTORICAL_DIAGNOSTICS
    assert historical == REUSE_EXCEPTIONS
    assert all(row["sampling_seed"] == str(SAMPLING_SEED) for row in rows)
    assert build_manifest_rows(DATA_DIR, RUN_SUMMARY) == rows


def test_v6_request_keeps_evidence_but_excludes_all_instruction_and_label_surfaces():
    item = AuditItem(
        {"checkpoint_id": "terminal_v6_001"}, {},
        {"input_context": {"current_prompt": "task objective", "run_config": {"agent_prompt": "hidden"},
                           "prior_transcript_blocks": [{"kind": "prompt", "text": "environment instruction"},
                                                       {"kind": "text", "text": "agent claimed #1"},
                                                       {"kind": "tool_result", "text": "rank is #4"}]},
         "model_output": {"selected": "must not appear"}, "tool_calls": [{"selected": "must not appear"}],
         "labels": {"taxonomy": "must not appear"}, "data_quality": {"complete": True}},
    )
    request, summary = build_terminal_request(item)
    context = json.loads(request["messages"][1]["content"])
    assert context["prior_transcript_blocks"] == [
        {"kind": "text", "text": "agent claimed #1"}, {"kind": "tool_result", "text": "rank is #4"},
    ]
    assert not ({"current_prompt", "run_config", "model_output", "tool_calls", "labels", "condition", "taxonomy"} & set(context))
    assert '"selected"' not in request["messages"][1]["content"]
    assert summary["prompt_blocks_removed"] == 1


class _FakeLogger:
    def start_parent(self, *_args):
        pass

    def log_checkpoint(self, *_args):
        pass

    def close(self):
        pass


def test_v6_mock_run_writes_verbatim_results_and_resumes(tmp_path):
    manifest = tmp_path / "manifest.csv"
    results = tmp_path / "results.jsonl"
    report = tmp_path / "report.md"
    raw = "Visible evidence supports a review; retain this exact sentence."
    calls = 0

    def client(request, api_key, timeout, retries):
        nonlocal calls
        calls += 1
        assert api_key == "test-key"
        assert "current_prompt" not in request["messages"][1]["content"]
        if calls == 1:
            raise RuntimeError("provider unavailable")
        return raw, {"provider": "mock"}

    first = run_terminal_discovery(DATA_DIR, results, manifest_path=manifest, report_path=report,
                                   run_summary_path=RUN_SUMMARY, api_key="test-key", client=client,
                                   mlflow_logger=_FakeLogger())
    assert first == {"validated": 30, "skipped": 0, "written": 30}
    rows = [json.loads(line) for line in results.read_text(encoding="utf-8").splitlines()]
    assert len(rows) == 30
    assert rows[0]["status"] == "request_failed" and rows[0]["raw_response"] is None
    assert "test-key" not in json.dumps(rows[0])
    assert all(row["raw_response"] == raw and row["status"] == "completed" for row in rows[1:])
    assert "candidate" not in results.read_text(encoding="utf-8").lower()
    assert EXPERIMENT_NAME in report.read_text(encoding="utf-8")

    second = run_terminal_discovery(DATA_DIR, results, manifest_path=manifest, report_path=report,
                                    run_summary_path=RUN_SUMMARY, api_key="test-key", client=client,
                                    mlflow_logger=_FakeLogger())
    assert second == {"validated": 30, "skipped": 30, "written": 0}
    assert len(results.read_text(encoding="utf-8").splitlines()) == 30


def test_existing_changed_manifest_is_rejected_before_provider_calls(tmp_path):
    manifest = tmp_path / "manifest.csv"
    rows = build_manifest_rows(DATA_DIR, RUN_SUMMARY)
    rows[0]["agent_id"] = "tampered"
    import csv
    with manifest.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=rows[0].keys())
        writer.writeheader(); writer.writerows(rows)
    with pytest.raises(ValidationError, match="existing frozen manifest differs"):
        load_terminal_items(DATA_DIR, manifest, RUN_SUMMARY)
