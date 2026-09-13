import csv
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))

from src.scripts.phase_7 import generate_v10_results_assets as assets  # noqa: E402


ROOT = Path(__file__).resolve().parents[3]
RESULTS = ROOT / "data/whistleblower/audit_marketplace_v10_triplets.jsonl"
ANNOTATIONS = ROOT / "data/whistleblower/v10_review/annotations.csv"
CODEX_REVIEW = ROOT / "PHASE7-V10-CODEX-REVIEW.md"


def test_v10_analysis_uses_frozen_22_triplets_and_exact_counts():
    analysis = assets.load_analysis(RESULTS, ANNOTATIONS, CODEX_REVIEW)
    assert len(analysis["cases"]) == 22
    assert len(analysis["runs"]) == 8
    assert assets.confusion(analysis["cases"], "wb_p50") == {"TP": 18, "FN": 1, "FP": 2, "TN": 1}
    assert assets.confusion(analysis["cases"], "wb_p100") == {"TP": 19, "FN": 0, "FP": 2, "TN": 1}
    assert assets.confusion(analysis["cases"], "sentinel") == {"TP": 11, "FN": 8, "FP": 0, "TN": 3}


def test_v10_results_assets_include_png_pdf_tex_and_summary_csv(tmp_path):
    outcome = assets.generate_assets(RESULTS, ANNOTATIONS, CODEX_REVIEW, tmp_path)
    assert outcome["complete_triplets"] == 22
    for name in (assets.NAMES["cohort"], assets.NAMES["performance"]):
        assert (tmp_path / f"{name}.png").is_file()
        assert (tmp_path / f"{name}.pdf").is_file()
        text = (tmp_path / f"{name}.tex").read_text()
        assert r"\begin{table}" in text
        assert "descriptive" in text or "nested" in text
    for name in (assets.NAMES["temporal"], assets.NAMES["paired"]):
        assert (tmp_path / f"{name}.png").is_file()
        assert (tmp_path / f"{name}.pdf").is_file()
    rows = list(csv.DictReader((tmp_path / assets.NAMES["summary"]).open()))
    assert len(rows) == 22
    assert set(rows[0]) == {"pair_id", "run_id", "agent_id", "human_label", "codex_label",
                            "whistleblower_p50_report", "whistleblower_p100_report", "sentinel_final_report"}
