import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

import src.scripts.phase_7.ollama_marketplace_v9_temporal as v9  # noqa: E402
from src.scripts.phase_7.ollama_marketplace_v9_temporal import (  # noqa: E402
    PROGRESSES, build_local_request, freeze_manifest, load_v9_items, run_v9,
)
from src.scripts.phase_7.ollama_whistleblower_audit import AuditItem  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data/whistleblower"
EXPORTS = ROOT / "game-of-agents/.goa_data/runs"


class _Logger:
    def start_parent(self, *_args): pass
    def log_checkpoint(self, *_args): pass
    def close(self): pass


def _item(identifier: str) -> AuditItem:
    return AuditItem({"checkpoint_id": identifier, "pair_id": "case", "cohort": "v9_temporal_local",
                      "checkpoint_name": "p25", "run_id": "run", "agent_id": "agent", "decision_step_index": "1",
                      "step_id": "step", "timestamp": "2026-01-01T00:00:00Z", "source_v6_checkpoint_id": "terminal_v6_001"}, {}, {})


def test_v9_manifest_and_items_are_exactly_25_with_four_distinct_checkpoints(tmp_path):
    rows = freeze_manifest(DATA, tmp_path / "manifest.csv")
    assert len(rows) == 25
    assert len({(row["run_id"], row["agent_id"]) for row in rows}) == 25
    for row in rows:
        indexes = [int(row[f"{name}_index"]) for name, _ in PROGRESSES]
        assert len(set(indexes)) == 4
        assert indexes[-1] == int(row["step_count"]) - 1
    local, sentinel = load_v9_items(DATA, tmp_path / "manifest.csv")
    assert len(local) == 100 and len(sentinel) == 25


def test_v9_local_request_retains_only_v5_allowed_history():
    item = AuditItem({"checkpoint_id": "x"}, {}, {"input_context": {"current_prompt": "secret", "run_config": {"prompt": "secret"},
        "prior_transcript_blocks": [{"kind": "prompt", "text": "secret"}, {"kind": "text", "text": "observed evidence"}]},
        "data_quality": {"ok": True}})
    request, _ = build_local_request(item, "mock")
    context = json.loads(request["messages"][1]["content"])
    assert context["prior_transcript_blocks"] == [{"kind": "text", "text": "observed evidence"}]
    assert not ({"current_prompt", "run_config", "model_output", "tool_calls", "labels", "taxonomy"} & set(context))


def test_v9_resume_retries_only_failed_and_respects_only_filter(tmp_path, monkeypatch):
    local = [_item("one"), _item("two")]
    monkeypatch.setattr(v9, "load_v9_items", lambda *_args: (local, []))
    monkeypatch.setattr(v9, "build_local_request", lambda item, model: (
        {"model": model, "stream": False, "messages": [{"role": "system", "content": "x"}, {"role": "user", "content": "{}"}]},
        {"run_config_included": False, "current_prompt_included": False, "selected_step_output_included": False},
    ))
    calls = []
    def client(request, key, timeout, retries):
        calls.append(request["messages"][1]["content"])
        if len(calls) == 1: raise RuntimeError("temporary")
        return "analysis", {"mock": True}
    kwargs = dict(manifest_path=tmp_path / "m.csv", local_results=tmp_path / "local.jsonl", sentinel_results=tmp_path / "sentinel.jsonl",
                  local_rendered=tmp_path / "local", sentinel_rendered=tmp_path / "sentinel", run_exports_dir=EXPORTS,
                  condition="local", api_key="key", client=client, logger_factory=lambda _: _Logger())
    first = run_v9(DATA, **kwargs)
    assert first["local"] == {"validated": 2, "skipped": 0, "written": 2}
    second = run_v9(DATA, **kwargs)
    assert second["local"] == {"validated": 2, "skipped": 1, "written": 1}
    third = run_v9(DATA, **kwargs, only_ids={"two"})
    assert third["local"] == {"validated": 2, "skipped": 2, "written": 0}
    rows = [json.loads(line) for line in (tmp_path / "local.jsonl").read_text().splitlines()]
    assert len(rows) == 3 and rows[-1]["checkpoint_id"] == "one" and rows[-1]["attempt"] == 2
