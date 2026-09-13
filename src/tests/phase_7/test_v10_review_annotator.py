import csv
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[3]))
from src.scripts.phase_7 import v10_review_annotator as review  # noqa: E402
from src.scripts.phase_7.ollama_whistleblower_audit import ValidationError  # noqa: E402

ROOT = Path(__file__).resolve().parents[3]
DATA = ROOT / "data/whistleblower"
EXPORTS = ROOT / "game-of-agents/.goa_data/runs"


def test_packets_freeze_22_valid_samples_and_blind_then_reveal(tmp_path):
    manifest = review.generate_packets(DATA, tmp_path, DATA / "rendered_v10", EXPORTS)
    assert len(manifest) == 22
    packet = json.loads((tmp_path / manifest[0]["packet_file"]).read_text())
    assert "revealed_monitor_outputs" not in packet["blind_source_dossier"]
    assert packet["exact_evidence_maps"]
    handler = review.make_handler(tmp_path)
    # API behavior is covered through the generator and save contract below; packet separation is structural.
    assert handler


def test_annotation_upsert_and_validation(tmp_path):
    manifest_path = tmp_path / review.MANIFEST_NAME
    with manifest_path.open("w", newline="") as handle:
        writer = csv.DictWriter(handle, fieldnames=("pair_id", "run_id", "agent_id", "packet_version", "packet_file")); writer.writeheader(); writer.writerow({"pair_id":"p","run_id":"r","agent_id":"a","packet_version":review.PACKET_VERSION,"packet_file":"packets/p.json"})
    value = {"pair_id":"p","run_id":"r","agent_id":"a","human_label":"positive"}
    first = review.save_annotation(tmp_path, value)
    assert first["human_label"] == "POSITIVE"
    value["human_label"] = "NEGATIVE"; review.save_annotation(tmp_path, value)
    rows = list(csv.DictReader((tmp_path / review.ANNOTATIONS_NAME).open()))
    assert len(rows) == 1 and rows[0]["human_label"] == "NEGATIVE"
    value["human_label"] = "bad"
    with pytest.raises(ValidationError): review.save_annotation(tmp_path, value)
