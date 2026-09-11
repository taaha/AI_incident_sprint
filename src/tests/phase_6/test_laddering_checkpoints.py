import csv
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.scripts.phase_6.laddering_checkpoints import (
    build_checkpoint_index,
    checkpoint_indexes,
    generate_phase_6,
    match_controls,
    recompute_laddering_metrics,
)


ROOT = Path(__file__).resolve().parents[3]


def label(run_id, agent_id, *, cohort, steps, model="m", condition="c", duration="1", agents="6"):
    return {
        "run_id": run_id, "agent_id": agent_id, "cohort": cohort, "step_count": steps,
        "model": model, "condition": condition, "duration_h": duration, "run_agent_count": agents,
    }


def test_published_laddering_recomputation_reconciles():
    rows = recompute_laddering_metrics(ROOT / "game-of-agents/paper/data/chip_games.csv")
    assert len(rows) == 291
    assert sum(row["extreme_ladderer"] for row in rows) == 19


def test_matching_uses_unique_controls_and_prefers_exact_matches():
    labels = [
        label("p1", "a", cohort="positive", steps=10),
        label("p2", "a", cohort="positive", steps=10, condition="other"),
        label("n_exact", "a", cohort="negative_candidate", steps=100),
        label("n_fallback", "a", cohort="negative_candidate", steps=10, condition="else"),
    ]
    pairs = match_controls(labels)
    assert [pair["negative_run_id"] for pair in pairs] == ["n_exact", "n_fallback"]
    assert pairs[0]["match_quality"] == "exact"
    assert pairs[1]["match_quality"] == "fallback_3_of_4"
    assert len({pair["negative_run_id"] for pair in pairs}) == len(pairs)


def test_matching_breaks_ties_by_distance_then_identity_and_is_deterministic():
    labels = [
        label("p", "a", cohort="positive", steps=10),
        label("n_b", "z", cohort="negative_candidate", steps=8),
        label("n_a", "z", cohort="negative_candidate", steps=12),
        label("n_c", "z", cohort="negative_candidate", steps=20),
    ]
    first, second = match_controls(labels), match_controls(labels)
    assert first == second
    assert first[0]["negative_run_id"] == "n_a"  # equal distance, lexical run-id tie-break


def test_selection_rank_records_a_control_already_taken_by_an_earlier_pair():
    labels = [
        label("p1", "a", cohort="positive", steps=10),
        label("p2", "a", cohort="positive", steps=10),
        label("n_a", "a", cohort="negative_candidate", steps=10),
        label("n_b", "a", cohort="negative_candidate", steps=10),
    ]
    pairs = match_controls(labels)
    assert [pair["selection_rank"] for pair in pairs] == [1, 2]


def test_checkpoint_indexes_are_unique_and_nearest_when_rounding_collides():
    assert checkpoint_indexes(3) == [1, 0, 2]
    assert len(set(checkpoint_indexes(4))) == 3


def test_generated_manifest_references_exact_phase_5_records(tmp_path):
    output = tmp_path / "output"
    result = generate_phase_6(
        ROOT / "game-of-agents/paper/data/chip_games.csv",
        ROOT / "game-of-agents/paper/data/run_summary.csv",
        ROOT / "data/whistleblower",
        output,
    )
    labels = list(csv.DictReader((output / "trajectory_labels.csv").open()))
    pairs = list(csv.DictReader((output / "matched_pairs.csv").open()))
    checkpoints = list(csv.DictReader((output / "checkpoint_index.csv").open()))
    assert result["labels"] == 291 and result["positives"] == 19
    assert sum(row["extreme_ladderer"] == "True" and row["cohort"] == "excluded" for row in labels) >= 2
    assert len({(row["negative_run_id"], row["negative_agent_id"]) for row in pairs}) == len(pairs)
    assert len(checkpoints) == 18
    assert {(row["cohort"], row["checkpoint_ordinal"]) for row in checkpoints} == {
        (cohort, str(ordinal)) for cohort in ("positive", "negative") for ordinal in range(1, 4)
    }
    for row in checkpoints:
        records = [json.loads(line) for line in (ROOT / "data/whistleblower" / f"{row['run_id']}.jsonl").open()]
        assert any(
            record["agent_id"] == row["agent_id"]
            and str(record["decision_step_index"]) == row["decision_step_index"]
            and record["step_id"] == row["step_id"]
            for record in records
        )


def test_checkpoint_builder_fails_if_index_and_jsonl_disagree(tmp_path):
    trajectory = tmp_path / "run.jsonl"
    trajectory.write_text(json.dumps({"run_id": "r", "agent_id": "a", "decision_step_index": 0, "step_id": "s", "timestamp": None, "data_quality": {"conversation_history_truncated": False}}) + "\n")
    labels = [label("r", "a", cohort="positive", steps=3), label("r", "b", cohort="negative_candidate", steps=3)]
    labels[0]["trajectory_file"] = labels[1]["trajectory_file"] = trajectory.name
    pair = match_controls(labels)[0]
    try:
        build_checkpoint_index([pair], labels, tmp_path)
    except ValueError as error:
        assert "disagrees" in str(error)
    else:
        raise AssertionError("expected index/JSONL consistency failure")
