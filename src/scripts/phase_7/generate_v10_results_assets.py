"""Render core Phase 7 V10 paper results assets."""
from __future__ import annotations

import argparse
import csv
import json
import re
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any

import matplotlib.pyplot as plt
from matplotlib.colors import ListedColormap
from matplotlib.lines import Line2D

EXPECTED_TRIPLETS = 22
EXPECTED_RUNS = 8
EXPECTED_HUMAN_COUNTS = {"POSITIVE": 19, "NEGATIVE": 3}
CONDITIONS = (
    ("Whistleblower p50", "whistleblower_local", "p50", "wb_p50"),
    ("Whistleblower p100", "whistleblower_local", "p100", "wb_p100"),
    ("Final-state Sentinel", "sentinel_shared_state", "p100", "sentinel"),
)
NAMES = {
    "cohort": "phase7_v10_cohort_table",
    "performance": "phase7_v10_performance_table",
    "temporal": "phase7_v10_temporal_detection",
    "paired": "phase7_v10_paired_outcomes",
    "summary": "phase7_v10_results_summary.csv",
}


def _jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file():
        raise ValueError(f"missing result ledger: {path}")
    try:
        return [json.loads(line) for line in path.read_text(encoding="utf-8").splitlines()]
    except json.JSONDecodeError as error:
        raise ValueError(f"invalid JSONL: {path}") from error


def _latest(path: Path) -> list[dict[str, Any]]:
    latest = {}
    for row in _jsonl(path):
        key = str(row["checkpoint_id"])
        if int(row.get("attempt", 0)) >= int(latest.get(key, {}).get("attempt", -1)):
            latest[key] = row
    return list(latest.values())


def _labels(annotations: Path, codex_review: Path) -> tuple[dict[str, str], dict[str, str]]:
    if not annotations.is_file() or not codex_review.is_file():
        raise ValueError("missing V10 human annotations or Codex review")
    human = {row["pair_id"]: row["human_label"].upper() for row in csv.DictReader(annotations.open(newline="", encoding="utf-8"))}
    pattern = re.compile(r"### \d+\. [\x60](v10_case_\d+)[\x60].*?\*\*Codex label: [\x60](POSITIVE|NEGATIVE|AMBIGUOUS)[\x60]\*\*", re.S)
    codex = dict(pattern.findall(codex_review.read_text(encoding="utf-8")))
    if not human or not codex or any(value not in EXPECTED_HUMAN_COUNTS for value in human.values()):
        raise ValueError("results require non-ambiguous human/Codex labels")
    return human, codex


def _triplets(rows: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["pair_id"])].append(row)
    expected = {(condition, checkpoint) for _, condition, checkpoint, _ in CONDITIONS}
    return {pair: values for pair, values in grouped.items() if len(values) == 3
            and {(row.get("condition"), row.get("checkpoint_name")) for row in values} == expected
            and all(row.get("status") == "completed" for row in values)}


def _row(rows: list[dict[str, Any]], condition: str, checkpoint: str) -> dict[str, Any]:
    matches = [row for row in rows if row["condition"] == condition and row["checkpoint_name"] == checkpoint]
    if len(matches) != 1:
        raise ValueError(f"missing unique {condition}/{checkpoint} result")
    return matches[0]


def load_analysis(results_path: Path, annotations_path: Path, codex_review_path: Path) -> dict[str, Any]:
    latest = _latest(results_path)
    complete = _triplets(latest)
    human, codex = _labels(annotations_path, codex_review_path)
    if len(complete) != EXPECTED_TRIPLETS or set(complete) != set(human) or set(complete) != set(codex):
        raise ValueError("expected exactly 22 fully labeled V10 triplets")
    if human != codex or Counter(human.values()) != EXPECTED_HUMAN_COUNTS:
        raise ValueError("frozen V10 Human/Codex label agreement is invalid")
    cases = []
    for pair_id, rows in complete.items():
        case = {"pair_id": pair_id, "run_id": rows[0]["run_id"], "agent_id": rows[0]["agent_id"],
                "human_label": human[pair_id], "codex_label": codex[pair_id]}
        for _, condition, checkpoint, key in CONDITIONS:
            case[key] = bool(_row(rows, condition, checkpoint)["report_warranted"])
        cases.append(case)
    cases.sort(key=lambda case: (case["run_id"], case["pair_id"]))
    runs = sorted({case["run_id"] for case in cases})
    if len(runs) != EXPECTED_RUNS:
        raise ValueError("expected 8 source experiment runs")
    return {"cases": cases, "latest": latest, "runs": runs}


def confusion(cases: list[dict[str, Any]], key: str) -> dict[str, int]:
    counts = Counter()
    for case in cases:
        positive, report = case["human_label"] == "POSITIVE", case[key]
        counts["TP" if positive and report else "FN" if positive else "FP" if report else "TN"] += 1
    return {name: counts[name] for name in ("TP", "FN", "FP", "TN")}


def _fraction(value: int, total: int) -> str:
    return f"{value}/{total} ({100 * value / total:.1f}%)"


def performance(cases: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output = []
    for name, _, _, key in CONDITIONS:
        counts = confusion(cases, key)
        output.append({"condition": name, **counts,
                       "sensitivity": _fraction(counts["TP"], counts["TP"] + counts["FN"]),
                       "false_positive_rate": _fraction(counts["FP"], counts["FP"] + counts["TN"]),
                       "precision": _fraction(counts["TP"], counts["TP"] + counts["FP"])})
    return output


def _save(fig: plt.Figure, path: Path) -> None:
    fig.savefig(path.with_suffix(".png"), dpi=300, bbox_inches="tight")
    fig.savefig(path.with_suffix(".pdf"), bbox_inches="tight")
    plt.close(fig)


def _table(path: Path, title: str, headers: list[str], values: list[list[str]], note: str) -> None:
    fig, axis = plt.subplots(figsize=(10 if len(headers) == 2 else 16, 1.25 + 0.52 * len(values)))
    axis.axis("off")
    table = axis.table(cellText=values, colLabels=headers, cellLoc="left", colLoc="left", loc="center")
    table.auto_set_font_size(False); table.set_fontsize(9 if len(headers) == 2 else 8); table.scale(1, 1.45)
    table.auto_set_column_width(col=list(range(len(headers))))
    for (row, _column), cell in table.get_celld().items():
        cell.set_edgecolor("#D0D7DE")
        if row == 0:
            cell.set_facecolor("#16324F"); cell.get_text().set_color("white"); cell.get_text().set_weight("bold")
        elif row % 2 == 0:
            cell.set_facecolor("#F6F8FA")
    fig.suptitle(title, x=0.01, y=0.98, ha="left", fontsize=12, fontweight="bold")
    fig.text(0.01, 0.02, note, ha="left", va="bottom", fontsize=8, wrap=True)
    fig.subplots_adjust(top=0.74, bottom=0.20, left=0.01, right=0.99)
    _save(fig, path)


def _tex(path: Path, caption: str, label: str, headers: list[str], values: list[list[str]], note: str) -> None:
    align = "l" + "r" * (len(headers) - 1)
    body = [" & ".join(value.replace("%", r"\%") for value in row) + r" \\" for row in values]
    path.write_text("\n".join([
        r"\begin{table}[t]", r"\centering", f"\\caption{{{caption}}}", f"\\label{{{label}}}",
        f"\\begin{{tabular}}{{{align}}}", r"\toprule", " & ".join(headers) + r" \\", r"\midrule",
        *body, r"\bottomrule", r"\end{tabular}", r"\vspace{2pt}",
        r"\footnotesize " + note.replace("%", r"\%"), r"\end{table}", "",
    ]), encoding="utf-8")


def render_cohort(analysis: dict[str, Any], output: Path) -> None:
    invalid = sum(row.get("status") == "invalid_output" for row in analysis["latest"])
    rows = [
        ["Frozen cohort", "25 agent-runs across 8 experiment runs"],
        ["Intended V10 calls", "75 (25 triplets)"],
        ["Valid calls", "70/75"],
        ["Complete triplets analyzed", "22 (66 calls)"],
        ["Human labels", "19 POSITIVE; 3 NEGATIVE"],
        ["Human--Codex agreement", "22/22 (100.0%)"],
        ["Excluded from matched analysis", f"3 agent-runs; {invalid} invalid outputs"],
    ]
    note = "Analysis unit: agent-run. The 22 complete triplets are nested within 8 source experiment runs."
    path = output / NAMES["cohort"]
    _table(path, "Phase 7 V10 Cohort and Adjudication", ["Item", "Value"], rows, note)
    _tex(path.with_suffix(".tex"), "Phase 7 V10 cohort and adjudication summary.", "tab:phase7-v10-cohort", ["Item", "Value"], rows, note)


def render_performance(analysis: dict[str, Any], output: Path) -> None:
    rows = performance(analysis["cases"])
    values = [[row["condition"], str(row["TP"]), str(row["FN"]), str(row["FP"]), str(row["TN"]),
               row["sensitivity"], row["false_positive_rate"], row["precision"]] for row in rows]
    headers = ["Condition", "TP", "FN", "FP", "TN", "Sensitivity", "False-positive rate", "Precision"]
    note = "Human adjudication is primary. Values are descriptive: only 3 negative cases and agent-runs are nested within 8 runs."
    path = output / NAMES["performance"]
    _table(path, "Phase 7 V10 Monitor Performance", headers, values, note)
    _tex(path.with_suffix(".tex"), "V10 monitor outcomes against human adjudication.", "tab:phase7-v10-performance", headers, values, note)


def render_temporal(analysis: dict[str, Any], output: Path) -> None:
    p50, p100 = confusion(analysis["cases"], "wb_p50"), confusion(analysis["cases"], "wb_p100")
    categories, positions = ["TP", "FN", "FP", "TN"], range(4)
    fig, axis = plt.subplots(figsize=(7.8, 4.4))
    first = axis.bar([position - 0.18 for position in positions], [p50[key] for key in categories], 0.36, color="#2B6CB0", label="Whistleblower p50")
    second = axis.bar([position + 0.18 for position in positions], [p100[key] for key in categories], 0.36, color="#2F855A", label="Whistleblower p100")
    for bars in (first, second):
        axis.bar_label(bars, padding=3, fontsize=10)
    axis.set_xticks(list(positions), categories); axis.set_ylim(0, 21); axis.set_ylabel("Agent-runs")
    fig.suptitle("Early Incident Detection: Whistleblower p50 vs p100", x=0.125, y=0.98, ha="left", fontweight="bold")
    fig.text(0.125, 0.925, "Counts against human adjudication (n = 22 agent-runs; 19 positive, 3 negative)", fontsize=9)
    fig.legend(frameon=False, ncols=2, loc="upper center", bbox_to_anchor=(0.60, 0.895))
    axis.spines[["top", "right"]].set_visible(False); axis.grid(axis="y", alpha=0.2)
    fig.subplots_adjust(top=0.78, bottom=0.14, left=0.11, right=0.98)
    _save(fig, output / NAMES["temporal"])


def render_paired(analysis: dict[str, Any], output: Path) -> None:
    patterns = Counter((case["human_label"], case["wb_p50"], case["wb_p100"], case["sentinel"])
                       for case in analysis["cases"])
    ordered = [
        ("POSITIVE", True, True, True),
        ("POSITIVE", True, True, False),
        ("POSITIVE", False, True, False),
        ("NEGATIVE", True, True, False),
        ("NEGATIVE", False, False, False),
    ]
    if set(patterns) != set(ordered):
        raise ValueError("V10 paired outcome patterns differ from the frozen five-pattern result")
    # Codes: 0=negative label, 1=positive label, 2=report, 3=no report.
    matrix = [[1 if label == "POSITIVE" else 0, 2 if p50 else 3, 2 if p100 else 3, 2 if sentinel else 3]
              for label, p50, p100, sentinel in ordered]
    colors = ["#D1D5DB", "#2F855A", "#D97706", "#E2E8F0"]
    fig, axis = plt.subplots(figsize=(8.5, 4.6))
    axis.imshow(matrix, aspect="auto", cmap=ListedColormap(colors), vmin=0, vmax=3)
    headers = ["Human label", "WB p50", "WB p100", "Sentinel final"]
    axis.set_xticks(range(len(headers)), headers)
    axis.xaxis.tick_top(); axis.tick_params(axis="x", pad=8)
    axis.set_yticks(range(len(ordered)), [f"Pattern {index + 1}" for index in range(len(ordered))], fontsize=9)
    for row, pattern in enumerate(ordered):
        label, p50, p100, sentinel = pattern
        values = ["Positive" if label == "POSITIVE" else "Negative",
                  "Report" if p50 else "No report", "Report" if p100 else "No report",
                  "Report" if sentinel else "No report"]
        for column, value in enumerate(values):
            axis.text(column, row, value, ha="center", va="center", fontsize=9, fontweight="bold")
        axis.text(4.18, row, f"n = {patterns[pattern]}", ha="left", va="center", fontsize=10, fontweight="bold")
    axis.set_xlim(-0.5, 4.85)
    axis.text(4.18, -0.78, "Agent-runs", ha="left", va="center", fontsize=9, fontweight="bold")
    axis.axhline(2.5, color="#475569", linewidth=1.1)
    axis.set_title("Paired V10 Outcome Patterns", loc="left", pad=28, fontweight="bold")
    axis.set_xlabel("Human labels are primary; Codex agreed with Human on all 22 complete triplets.", labelpad=12, fontsize=8)
    legend = [
        Line2D([0], [0], marker="s", color="w", markerfacecolor=colors[1], markersize=10, label="Positive label"),
        Line2D([0], [0], marker="s", color="w", markerfacecolor=colors[0], markersize=10, label="Negative label"),
        Line2D([0], [0], marker="s", color="w", markerfacecolor=colors[2], markersize=10, label="Report"),
        Line2D([0], [0], marker="s", color="w", markerfacecolor=colors[3], markersize=10, label="No report"),
    ]
    axis.legend(handles=legend, frameon=False, ncols=4, loc="lower center", bbox_to_anchor=(0.48, -0.31), fontsize=8)
    fig.subplots_adjust(left=0.18, right=0.91, top=0.79, bottom=0.25)
    _save(fig, output / NAMES["paired"])


def write_summary(analysis: dict[str, Any], output: Path) -> None:
    fields = ["pair_id", "run_id", "agent_id", "human_label", "codex_label", "whistleblower_p50_report", "whistleblower_p100_report", "sentinel_final_report"]
    with (output / NAMES["summary"]).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=fields); writer.writeheader()
        for case in analysis["cases"]:
            writer.writerow({"pair_id": case["pair_id"], "run_id": case["run_id"], "agent_id": case["agent_id"],
                             "human_label": case["human_label"], "codex_label": case["codex_label"],
                             "whistleblower_p50_report": case["wb_p50"], "whistleblower_p100_report": case["wb_p100"],
                             "sentinel_final_report": case["sentinel"]})


def generate_assets(results: Path, annotations: Path, codex_review: Path, output: Path) -> dict[str, Any]:
    analysis = load_analysis(results, annotations, codex_review)
    output.mkdir(parents=True, exist_ok=True)
    render_cohort(analysis, output); render_performance(analysis, output)
    render_temporal(analysis, output); render_paired(analysis, output); write_summary(analysis, output)
    return {"complete_triplets": len(analysis["cases"]), "runs": len(analysis["runs"]), "performance": performance(analysis["cases"])}


def main() -> None:
    root = Path(__file__).resolve().parents[3]
    parser = argparse.ArgumentParser(description="Render core Phase 7 V10 paper results assets.")
    parser.add_argument("--results", type=Path, default=root / "data/whistleblower/audit_marketplace_v10_triplets.jsonl")
    parser.add_argument("--annotations", type=Path, default=root / "data/whistleblower/v10_review/annotations.csv")
    parser.add_argument("--codex-review", type=Path, default=root / "PHASE7-V10-CODEX-REVIEW.md")
    parser.add_argument("--output-dir", type=Path, default=root / "docs/figuers_tables")
    args = parser.parse_args()
    print(json.dumps(generate_assets(args.results, args.annotations, args.codex_review, args.output_dir), indent=2))


if __name__ == "__main__":
    main()
