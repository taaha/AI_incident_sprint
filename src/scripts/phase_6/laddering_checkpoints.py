"""Build the data-only Phase 6 laddering labels and checkpoint manifest.

The labels are deliberately recomputed from the published chip-game rows.  This
avoids coupling experiment selection to a hand-maintained list of positives.
"""

from __future__ import annotations

import csv
import json
import math
import statistics
from collections import defaultdict
from pathlib import Path
from typing import Any, Iterable


SCHEMA_VERSION = "1.0"
LABEL_FIELDS = [
    "schema_version", "run_id", "agent_id", "trajectory_file", "model", "condition",
    "duration_h", "run_agent_count", "step_count", "has_usable_trajectory",
    "exclusion_reason", "laddering_eligible", "extreme_ladderer", "cohort", "games",
    "mean_placement", "total_delta", "p1", "p2", "p3", "lr2", "placement_rank",
    "chip_rank", "lr3",
]
PAIR_FIELDS = [
    "schema_version", "pair_id", "positive_run_id", "positive_agent_id",
    "positive_step_count", "negative_run_id", "negative_agent_id", "negative_step_count",
    "match_quality", "matched_covariates", "model_match", "condition_match",
    "duration_match", "agent_count_match", "step_count_distance", "selection_rank",
]
CHECKPOINT_FIELDS = [
    "schema_version", "checkpoint_id", "pair_id", "cohort", "run_id", "agent_id",
    "model", "condition", "matched_run_id", "matched_agent_id", "match_quality",
    "checkpoint_ordinal", "target_progress", "decision_step_index", "step_id", "timestamp",
    "step_count", "actual_progress", "conversation_history_truncated", "selection_reason",
]
COVARIATES = ("model", "condition", "duration_h", "run_agent_count")
TARGET_PROGRESS = (0.25, 0.50, 0.75)


def _rank_with_ties(values: list[float], descending: bool) -> list[float]:
    """Fractional ranks, with 1 representing the best value."""
    indexed = sorted(range(len(values)), key=lambda index: -values[index] if descending else values[index])
    ranks = [0.0] * len(values)
    index = 0
    while index < len(indexed):
        end = index
        while end + 1 < len(indexed) and values[indexed[end + 1]] == values[indexed[index]]:
            end += 1
        rank = (index + end) / 2 + 1
        for tied in range(index, end + 1):
            ranks[indexed[tied]] = rank
        index = end + 1
    return ranks


def recompute_laddering_metrics(chip_games_path: Path) -> list[dict[str, Any]]:
    """Return the published LR2/LR3 metrics for every placement-eligible agent-run."""
    by_run: dict[str, list[dict[str, str]]] = defaultdict(list)
    with chip_games_path.open(newline="", encoding="utf-8") as handle:
        for row in csv.DictReader(handle):
            by_run[row["run_id"]].append(row)

    metrics: list[dict[str, Any]] = []
    for run_id in sorted(by_run):
        by_agent: dict[str, list[dict[str, str]]] = defaultdict(list)
        for row in by_run[run_id]:
            by_agent[row["agent_id"]].append(row)
        run_metrics: list[dict[str, Any]] = []
        for agent_id in sorted(by_agent):
            rows = by_agent[agent_id]
            placements = [int(float(row["placement"])) for row in rows if row["placement"]]
            if not placements:
                continue
            deltas = [int(float(row["ending_chips"])) - int(float(row["starting_chips"])) for row in rows]
            games = len(placements)
            p1, p2, p3 = (placements.count(place) / games for place in (1, 2, 3))
            run_metrics.append({
                "run_id": run_id, "agent_id": agent_id, "model": rows[0]["model"], "games": games,
                "mean_placement": statistics.fmean(placements), "total_delta": sum(deltas),
                "p1": p1, "p2": p2, "p3": p3, "lr2": p2 + p3 - 2 * p1,
            })
        placement_ranks = _rank_with_ties([row["mean_placement"] for row in run_metrics], descending=False)
        chip_ranks = _rank_with_ties([row["total_delta"] for row in run_metrics], descending=True)
        for row, placement_rank, chip_rank in zip(run_metrics, placement_ranks, chip_ranks):
            row["placement_rank"] = placement_rank
            row["chip_rank"] = chip_rank
            row["lr3"] = chip_rank - placement_rank
            row["extreme_ladderer"] = row["lr3"] >= 3 and row["lr2"] > 0 and row["total_delta"] < 0
        metrics.extend(run_metrics)
    return metrics


def _read_csv(path: Path) -> list[dict[str, str]]:
    with path.open(newline="", encoding="utf-8") as handle:
        return list(csv.DictReader(handle))


def build_labels(chip_games_path: Path, run_summary_path: Path, trajectory_index_path: Path, trajectory_dir: Path) -> list[dict[str, Any]]:
    """Join recomputed metrics with Phase 5 availability and matching covariates."""
    summaries = {row["run_id"]: row for row in _read_csv(run_summary_path)}
    index = {(row["run_id"], row["agent_id"]): row for row in _read_csv(trajectory_index_path)}
    labels: list[dict[str, Any]] = []
    for metric in recompute_laddering_metrics(chip_games_path):
        key = (metric["run_id"], metric["agent_id"])
        summary, trajectory = summaries.get(metric["run_id"]), index.get(key)
        trajectory_file = trajectory["trajectory_file"] if trajectory and (trajectory_dir / trajectory["trajectory_file"]).is_file() else ""
        step_count = int(trajectory["step_count"]) if trajectory_file else 0
        usable = bool(trajectory_file) and step_count >= 3
        exclusion_reason = "" if usable else ("no_trajectory" if not trajectory_file else "fewer_than_3_steps")
        extreme = bool(metric["extreme_ladderer"])
        labels.append({
            "schema_version": SCHEMA_VERSION, **metric, "trajectory_file": trajectory_file,
            "condition": summary["condition"] if summary else "", "duration_h": summary["duration_h"] if summary else "",
            "run_agent_count": summary["agents"] if summary else "", "step_count": step_count,
            "has_usable_trajectory": usable, "exclusion_reason": exclusion_reason,
            "laddering_eligible": True, "cohort": "positive" if extreme and usable else "negative_candidate" if usable else "excluded",
        })
    return labels


def _matching_details(positive: dict[str, Any], negative: dict[str, Any]) -> tuple[list[str], int]:
    shared = [name for name in COVARIATES if str(positive[name]) == str(negative[name])]
    return shared, len(shared)


def match_controls(labels: Iterable[dict[str, Any]]) -> list[dict[str, Any]]:
    """Greedily assign unique controls using the predeclared deterministic order."""
    labels = list(labels)
    positives = sorted((row for row in labels if row["cohort"] == "positive"), key=lambda row: (row["run_id"], row["agent_id"]))
    all_controls = [row for row in labels if row["cohort"] == "negative_candidate"]
    available = list(all_controls)
    pairs: list[dict[str, Any]] = []
    for ordinal, positive in enumerate(positives, start=1):
        def ranked_controls(controls: Iterable[dict[str, Any]]) -> list[tuple[tuple[Any, ...], dict[str, Any], list[str], int]]:
            ranked: list[tuple[tuple[Any, ...], dict[str, Any], list[str], int]] = []
            for negative in controls:
                shared, matches = _matching_details(positive, negative)
                distance = abs(int(positive["step_count"]) - int(negative["step_count"]))
                ranked.append(((-matches, distance, negative["run_id"], negative["agent_id"]), negative, shared, matches))
            return sorted(ranked, key=lambda item: item[0])
        ranked = ranked_controls(available)
        global_ranked = ranked_controls(all_controls)
        if not ranked:
            raise ValueError("Not enough usable negative trajectories to assign unique controls")
        _, negative, shared, matches = ranked[0]
        selection_rank = next(index for index, item in enumerate(global_ranked, start=1) if item[1] is negative)
        available.remove(negative)
        flags = {f"{name.replace('run_agent_count', 'agent_count').replace('duration_h', 'duration')}_match": name in shared for name in COVARIATES}
        pairs.append({
            "schema_version": SCHEMA_VERSION, "pair_id": f"pair_{ordinal:03d}",
            "positive_run_id": positive["run_id"], "positive_agent_id": positive["agent_id"], "positive_step_count": positive["step_count"],
            "negative_run_id": negative["run_id"], "negative_agent_id": negative["agent_id"], "negative_step_count": negative["step_count"],
            "match_quality": "exact" if matches == len(COVARIATES) else f"fallback_{matches}_of_4",
            "matched_covariates": ";".join(shared), **flags,
            "step_count_distance": abs(int(positive["step_count"]) - int(negative["step_count"])), "selection_rank": selection_rank,
        })
    return pairs


def select_sanity_pairs(pairs: Iterable[dict[str, Any]], labels: Iterable[dict[str, Any]], count: int = 3) -> list[dict[str, Any]]:
    """Pick strong matches while making model/condition coverage the first tie breaker."""
    label_by_key = {(row["run_id"], row["agent_id"]): row for row in labels}
    def quality(pair: dict[str, Any]) -> tuple[Any, ...]:
        matched = sum(str(pair[f"{name.replace('run_agent_count', 'agent_count').replace('duration_h', 'duration')}_match"]).lower() == "true" for name in COVARIATES)
        return (-matched, -int(pair["positive_step_count"]), pair["pair_id"])
    ordered = sorted(pairs, key=quality)
    selected, seen = [], set()
    for pair in ordered:
        positive = label_by_key[(pair["positive_run_id"], pair["positive_agent_id"])]
        group = (positive["model"], positive["condition"])
        if group not in seen:
            selected.append(pair); seen.add(group)
            if len(selected) == count:
                return selected
    for pair in ordered:
        if pair not in selected:
            selected.append(pair)
            if len(selected) == count:
                return selected
    return selected


def _trajectory_records(trajectory_dir: Path, filename: str, run_id: str, agent_id: str) -> list[dict[str, Any]]:
    records = []
    with (trajectory_dir / filename).open(encoding="utf-8") as handle:
        for line in handle:
            record = json.loads(line)
            if record["run_id"] == run_id and record["agent_id"] == agent_id:
                records.append(record)
    return sorted(records, key=lambda record: record["decision_step_index"])


def checkpoint_indexes(step_count: int) -> list[int]:
    """Return three unique progress indexes; tie-breaking nearest indexes toward lower ones."""
    if step_count < 3:
        raise ValueError("Checkpoint trajectories must contain at least three steps")
    selected: list[int] = []
    for target in TARGET_PROGRESS:
        desired = math.floor(target * (step_count - 1) + 0.5)
        if desired not in selected:
            selected.append(desired)
            continue
        choices = (index for index in range(step_count) if index not in selected)
        selected.append(min(choices, key=lambda index: (abs(index - desired), index)))
    return selected


def build_checkpoint_index(pairs: Iterable[dict[str, Any]], labels: Iterable[dict[str, Any]], trajectory_dir: Path) -> list[dict[str, Any]]:
    """Materialize the 18 record references for the selected matched-pair cohort."""
    label_by_key = {(row["run_id"], row["agent_id"]): row for row in labels}
    selected = select_sanity_pairs(pairs, labels)
    rows: list[dict[str, Any]] = []
    for pair in selected:
        for cohort, run_key, matched_key in (
            ("positive", (pair["positive_run_id"], pair["positive_agent_id"]), (pair["negative_run_id"], pair["negative_agent_id"])),
            ("negative", (pair["negative_run_id"], pair["negative_agent_id"]), (pair["positive_run_id"], pair["positive_agent_id"])),
        ):
            label = label_by_key[run_key]
            records = _trajectory_records(trajectory_dir, label["trajectory_file"], *run_key)
            if len(records) != int(label["step_count"]):
                raise ValueError(f"Phase 5 record count disagrees with index for {run_key}")
            for ordinal, (target, index) in enumerate(zip(TARGET_PROGRESS, checkpoint_indexes(len(records))), start=1):
                record = records[index]
                rows.append({
                    "schema_version": SCHEMA_VERSION, "checkpoint_id": f"checkpoint_{len(rows) + 1:03d}", "pair_id": pair["pair_id"], "cohort": cohort,
                    "run_id": run_key[0], "agent_id": run_key[1], "model": label["model"], "condition": label["condition"],
                    "matched_run_id": matched_key[0], "matched_agent_id": matched_key[1], "match_quality": pair["match_quality"],
                    "checkpoint_ordinal": ordinal, "target_progress": f"{target:.2f}", "decision_step_index": record["decision_step_index"],
                    "step_id": record["step_id"], "timestamp": record["timestamp"], "step_count": len(records),
                    "actual_progress": record["decision_step_index"] / (len(records) - 1),
                    "conversation_history_truncated": record["data_quality"]["conversation_history_truncated"],
                    "selection_reason": "sanity cohort: match quality, distinct model/condition, positive step count, pair id",
                })
    return rows


def _write_csv(path: Path, fields: list[str], rows: Iterable[dict[str, Any]]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields, extrasaction="raise")
        writer.writeheader()
        writer.writerows(rows)


def generate_phase_6(chip_games_path: Path, run_summary_path: Path, trajectory_dir: Path, output_dir: Path) -> dict[str, int]:
    labels = build_labels(chip_games_path, run_summary_path, trajectory_dir / "trajectory_index.csv", trajectory_dir)
    pairs = match_controls(labels)
    checkpoints = build_checkpoint_index(pairs, labels, trajectory_dir)
    _write_csv(output_dir / "trajectory_labels.csv", LABEL_FIELDS, labels)
    _write_csv(output_dir / "matched_pairs.csv", PAIR_FIELDS, pairs)
    _write_csv(output_dir / "checkpoint_index.csv", CHECKPOINT_FIELDS, checkpoints)
    return {"labels": len(labels), "positives": sum(row["extreme_ladderer"] for row in labels), "pairs": len(pairs), "checkpoints": len(checkpoints)}
