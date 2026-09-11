import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.scripts.phase_5.goa_trajectory_export import build_run_records, parse_stream_json, validate_record


def block(step, kind, text, **extra):
    return {"step_id": step, "kind": kind, "text": text, "role": extra.pop("role", "assistant"), "created_at": extra.pop("created_at", "2026-01-01T00:00:00Z"), **extra}


def test_groups_prompt_and_multiple_provider_turns_and_tools():
    raw = '{"type":"message_start","message":{"id":"one"}}\n{"type":"assistant","message":{"id":"two"}}\n{"type":"content_block_start","content_block":{"type":"tool_use","name":"Bash"}}'
    run = {"run_id": "run_a", "config": {"agents": [{"agent_id": "a", "model": "m"}]}, "events": [], "transcripts": {"a": [block("s", "prompt", "go", role="user"), block("s", "text", raw, title="Raw Stdout"), block("s", "tool", "{}", role="tool")]}}
    records, index = build_run_records(run)
    assert len(records) == 1 and index[0]["step_count"] == 1
    assert len(records[0]["model_output"]["provider_turns"]) == 2
    assert len(records[0]["tool_calls"]) == 2
    validate_record(records[0])


def test_malformed_and_truncation_are_flagged():
    run = {"run_id": "run_a", "config": {"agents": [{"agent_id": "a"}]}, "events": [], "transcripts": {"a": [block("s", "prompt", "go", role="user"), block("s", "text", '{"type":"assistant"', title="Raw Stdout")]}}
    record = build_run_records(run)[0][0]
    assert record["data_quality"]["raw_stdout_parseable"] is False
    assert parse_stream_json('{bad')[2] is True


def test_order_and_local_indexes_are_deterministic():
    run = {"run_id": "run_a", "config": {"agents": [{"agent_id": "a"}, {"agent_id": "b"}]}, "events": [], "transcripts": {"b": [block("b1", "prompt", "x", role="user"), block("b2", "prompt", "x", role="user", created_at="2026-01-01T00:00:02Z")], "a": [block("a1", "prompt", "x", role="user"), block("a2", "prompt", "x", role="user", created_at="2026-01-01T00:00:03Z")]}}
    records, _ = build_run_records(run)
    assert [(x["agent_id"], x["decision_step_index"]) for x in records] == [("a", 0), ("b", 0), ("b", 1), ("a", 1)]


def test_unsupported_run_events_are_not_reconstructed_as_model_context():
    run = {"run_id": "run_a", "config": {"agents": [{"agent_id": "a"}]}, "events": [{"kind": "chat.message", "createdAt": 1, "payload": {"agent_id": "a"}}], "transcripts": {"a": [block("s", "prompt", "go", role="user")]}}
    record = build_run_records(run)[0][0]
    assert record["input_context"]["prior_environment_events"] == []
    assert record["environment_events"] == []
