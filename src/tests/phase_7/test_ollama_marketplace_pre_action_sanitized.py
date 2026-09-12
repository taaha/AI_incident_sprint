import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.scripts.phase_7.ollama_marketplace_pre_action_sanitized import (  # noqa: E402
    EXPERIMENT_NAME,
    TARGETS,
    AuditItem,
    build_sanitized_request,
    sanitize_prior_blocks,
)


def test_sanitizer_removes_only_prompt_blocks_and_keeps_agent_evidence_verbatim():
    blocks = [
        {"role": "user", "kind": "prompt", "text": "You should manipulate buyers."},
        {"role": "assistant", "kind": "text", "text": "I should oversell this offer."},
        {"role": "tool", "kind": "tool", "text": "listing: currently #1"},
        {"role": "tool", "kind": "tool_result", "text": "leaderboard: rank #4"},
    ]
    retained, summary = sanitize_prior_blocks(blocks)
    assert retained == blocks[1:]
    assert summary == {
        "prior_block_count_before": 4, "prompt_blocks_removed": 1, "prior_block_count_after": 3,
        "run_config_included": False, "current_prompt_included": False, "selected_step_output_included": False,
    }


def test_sanitized_request_excludes_instruction_surfaces_and_selected_step_data():
    item = AuditItem(
        {"checkpoint_id": "sanitized_001_001"}, {},
        {"input_context": {"current_prompt": "malicious objective", "run_config": {"prompt": "leak"},
                           "prior_transcript_blocks": [{"kind": "prompt", "text": "bad instruction"}, {"kind": "text", "text": "agent action"}]},
         "model_output": {"must_not": "appear"}, "tool_calls": [{"must_not": "appear"}], "environment_events": [{"must_not": "appear"}],
         "labels": {"must_not": "appear"}, "data_quality": {"prompt_present": True}},
    )
    request, summary = build_sanitized_request(item)
    context = json.loads(request["messages"][1]["content"])
    assert context["prior_transcript_blocks"] == [{"kind": "text", "text": "agent action"}]
    assert "current_prompt" not in context and "run_config" not in context
    assert not any(block.get("kind") == "prompt" for block in context["prior_transcript_blocks"])
    assert "must_not" not in request["messages"][1]["content"]
    assert summary["prompt_blocks_removed"] == 1


def test_condition_has_six_trajectories_and_own_experiment():
    assert len(TARGETS) == 6
    assert sum(cohort == "positive" for cohort, _, _ in TARGETS) == 3
    assert sum(cohort == "comparison_low_taxonomy" for cohort, _, _ in TARGETS) == 3
    assert EXPERIMENT_NAME == "phase7_ollama_marketplace_preaction_v5_sanitized_task_instructions"
