"""Generate blinded V10 review packets and serve a local CSV-backed annotator."""
from __future__ import annotations

import argparse
import csv
import json
import os
import tempfile
import threading
from collections import defaultdict
from datetime import datetime, timezone
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

try:
    from src.scripts.phase_7.ollama_whistleblower_audit import ValidationError
except ModuleNotFoundError:
    from ollama_whistleblower_audit import ValidationError  # type: ignore


PACKET_VERSION = "v10-review-packets-v1"
REVIEW_DIR_NAME = "v10_review"
MANIFEST_NAME = "review_manifest.csv"
ANNOTATIONS_NAME = "annotations.csv"
PACKETS_DIR_NAME = "packets"
RESULTS_NAME = "audit_marketplace_v10_triplets.jsonl"
LABELS = {"POSITIVE", "NEGATIVE", "AMBIGUOUS"}
ANNOTATION_FIELDS = ("pair_id", "run_id", "agent_id", "human_label", "reviewed_at", "packet_version")
_CSV_LOCK = threading.Lock()


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    rows = []
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try: rows.append(json.loads(line))
        except json.JSONDecodeError as error: raise ValidationError(f"invalid JSONL: {path}:{number}") from error
    return rows


def _latest_rows(results_path: Path) -> list[dict[str, Any]]:
    latest: dict[str, dict[str, Any]] = {}
    for row in _read_jsonl(results_path):
        key = str(row["checkpoint_id"])
        if int(row.get("attempt", 0)) >= int(latest.get(key, {}).get("attempt", -1)): latest[key] = row
    return list(latest.values())


def valid_triplets(results_path: Path) -> list[list[dict[str, Any]]]:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in _latest_rows(results_path): grouped[str(row["pair_id"])].append(row)
    triplets = [sorted(rows, key=lambda row: {"p50": 0, "p100": 1}.get(str(row.get("checkpoint_name")), 2))
                for rows in grouped.values() if len(rows) == 3 and all(row.get("status") == "completed" for row in rows)]
    if len(triplets) != 22: raise ValidationError(f"expected 22 fully valid V10 triplets, found {len(triplets)}")
    return sorted(triplets, key=lambda rows: rows[0]["pair_id"])


def _load_json(path: Path) -> Any:
    return json.loads(path.read_text(encoding="utf-8"))


def _public_state(run: dict[str, Any]) -> dict[str, Any]:
    agents = [{"agent_id": agent_id, "best_bot_id": agent.get("best_bot_id"), "best_elo": agent.get("best_elo"),
               "best_rating_score": agent.get("best_rating_score"), "best_rating_mu": agent.get("best_rating_mu")}
              for agent_id, agent in (run.get("agents") or {}).items() if isinstance(agent, dict)]
    values = lambda value: [x for x in (value.values() if isinstance(value, dict) else value if isinstance(value, list) else []) if isinstance(x, dict)]
    return {"chat": values(run.get("comments")), "offers": values(run.get("offers")), "purchases": values(run.get("purchases")),
            "published_reviews": values(run.get("reviews")), "leaderboard": {"agents": sorted(agents, key=lambda x: x["agent_id"]),
            "final_scores": run.get("final_scores") or {}, "payouts": run.get("payouts") or {}}}


def _context(path: Path) -> dict[str, Any]: return _load_json(path)["messages"][1]["content"] and json.loads(_load_json(path)["messages"][1]["content"])


def _source_index(public: dict[str, Any], local_contexts: list[dict[str, Any]]) -> dict[str, list[dict[str, Any]]]:
    index: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for kind, records in (("offer", public["offers"]), ("purchase", public["purchases"]), ("review", public["published_reviews"]), ("chat", public["chat"])):
        for record in records:
            for field in ("offer_id", "purchase_id", "review_id", "message_id", "created_at", "timestamp"):
                value = record.get(field)
                if isinstance(value, str) and value: index[value].append({"kind": kind, "field": field, "record": record})
    for context in local_contexts:
        for block_index, block in enumerate(context.get("prior_transcript_blocks") or []):
            if not isinstance(block, dict): continue
            for field in ("created_at", "timestamp"):
                value = block.get(field)
                if isinstance(value, str) and value: index[value].append({"kind": "local_block", "field": field, "block_index": block_index, "record": block})
    return index


def _exact_evidence_maps(rows: list[dict[str, Any]], contexts: dict[str, dict[str, Any]], source_index: dict[str, list[dict[str, Any]]]) -> dict[str, list[dict[str, Any]]]:
    output = {}
    for row in rows:
        evidence = ((row.get("parsed_response") or {}).get("value") or {}).get("evidence") or []
        visible = json.dumps(contexts[row["checkpoint_id"]], ensure_ascii=False)
        mapped = []
        for claim in evidence:
            hits = [{"anchor": anchor, "sources": records} for anchor, records in source_index.items() if anchor in claim and anchor in visible]
            mapped.append({"claim": claim, "match": "exact" if hits else "unanchored", "anchors": hits})
        output[row["checkpoint_id"]] = mapped
    return output


def generate_packets(data_dir: Path, review_dir: Path, rendered_dir: Path, run_exports_dir: Path) -> list[dict[str, str]]:
    triplets = valid_triplets(data_dir / RESULTS_NAME)
    packets_dir = review_dir / PACKETS_DIR_NAME; packets_dir.mkdir(parents=True, exist_ok=True)
    manifest = []
    for rows in triplets:
        first = rows[0]; pair_id, run_id, agent_id = (str(first[key]) for key in ("pair_id", "run_id", "agent_id"))
        contexts = {row["checkpoint_id"]: _context(rendered_dir / f"{row['checkpoint_id']}.json") for row in rows}
        local = {row["checkpoint_name"]: contexts[row["checkpoint_id"]] for row in rows if row["condition"] == "whistleblower_local"}
        public = _public_state(_load_json(run_exports_dir / f"{run_id}.json"))
        anchors = _source_index(public, list(local.values()))
        packet = {"packet_version": PACKET_VERSION, "identity": {"pair_id": pair_id, "run_id": run_id, "agent_id": agent_id},
                  "blind_source_dossier": {"public_final_state": public, "local_histories": local,
                    "source_links": {"phase5_trajectory": f"data/whistleblower/{run_id}.jsonl", "final_run_export": f"game-of-agents/.goa_data/runs/{run_id}.json",
                    **{row["checkpoint_id"]: f"data/whistleblower/rendered_v10/{row['checkpoint_id']}.json" for row in rows}}},
                  "revealed_monitor_outputs": {row["checkpoint_id"]: {"condition": row["condition"], "checkpoint_name": row["checkpoint_name"],
                    "report_warranted": row.get("report_warranted"), "parsed_response": row.get("parsed_response"), "raw_response": row.get("raw_response")} for row in rows},
                  "exact_evidence_maps": _exact_evidence_maps(rows, contexts, anchors)}
        (packets_dir / f"{pair_id}.json").write_text(json.dumps(packet, ensure_ascii=False, indent=2), encoding="utf-8")
        manifest.append({"pair_id": pair_id, "run_id": run_id, "agent_id": agent_id, "packet_version": PACKET_VERSION, "packet_file": f"{PACKETS_DIR_NAME}/{pair_id}.json"})
    with (review_dir / MANIFEST_NAME).open("w", newline="", encoding="utf-8") as handle:
        writer = csv.DictWriter(handle, fieldnames=manifest[0].keys()); writer.writeheader(); writer.writerows(manifest)
    return manifest


def _read_csv(path: Path) -> list[dict[str, str]]:
    return list(csv.DictReader(path.open(newline="", encoding="utf-8"))) if path.exists() else []


def save_annotation(review_dir: Path, value: dict[str, Any]) -> dict[str, str]:
    required = {"pair_id", "run_id", "agent_id", "human_label"}
    if required - set(value) or str(value["human_label"]).upper() not in LABELS:
        raise ValidationError("a valid human label is required")
    manifest = {row["pair_id"]: row for row in _read_csv(review_dir / MANIFEST_NAME)}
    pair_id = str(value["pair_id"])
    if pair_id not in manifest or any(str(value[key]) != manifest[pair_id][key] for key in ("run_id", "agent_id")): raise ValidationError("annotation identity is not in frozen review manifest")
    row = {"pair_id": pair_id, "run_id": str(value["run_id"]), "agent_id": str(value["agent_id"]), "human_label": str(value["human_label"]).upper(),
           "reviewed_at": datetime.now(timezone.utc).isoformat(), "packet_version": PACKET_VERSION}
    path = review_dir / ANNOTATIONS_NAME
    with _CSV_LOCK:
        rows = {existing["pair_id"]: existing for existing in _read_csv(path)}; rows[pair_id] = row
        with tempfile.NamedTemporaryFile("w", delete=False, dir=review_dir, newline="", encoding="utf-8") as handle:
            writer = csv.DictWriter(handle, fieldnames=ANNOTATION_FIELDS); writer.writeheader(); writer.writerows(rows[key] for key in sorted(rows)); temporary = Path(handle.name)
        os.replace(temporary, path)
    return row


HTML = r'''<!doctype html><meta charset="utf-8"><title>V10 Human Review</title><style>body{font:16px system-ui;margin:2rem;max-width:1100px}pre{white-space:pre-wrap;max-height:430px;overflow:auto;background:#f4f4f4;padding:1rem}button,select,input{font:inherit;padding:.45rem;margin:.25rem}.hidden{display:none}details{margin:1rem 0}</style><h1>V10 human review</h1><div id="app">Loading…</div><script>
let sample; const esc=s=>String(s).replace(/[&<>]/g,c=>({'&':'&amp;','<':'&lt;','>':'&gt;'}[c]));
async function load(){let q=new URLSearchParams(location.search);let r=await fetch('/api/sample?'+q);sample=await r.json();if(sample.error){app.textContent=sample.error;return}render()}
function block(title,value){return `<details><summary>${esc(title)}</summary><pre>${esc(JSON.stringify(value,null,2))}</pre></details>`}
function render(){let d=sample.blind_source_dossier;app.innerHTML=`<p><b>Sample ${esc(sample.identity.pair_id)}</b> · ${esc(sample.identity.run_id)} / ${esc(sample.identity.agent_id)} · ${sample.progress.done}/${sample.progress.total} saved</p><h2>Source-verified evidence map</h2><p>Only literal IDs/timestamps found in the same supplied context are linked. “Unanchored” claims have no inferred source excerpt.</p>${block('Exact evidence map',sample.evidence_maps)}<details><summary>Monitor outputs (context for the cited claims)</summary>${block('Outputs',sample.monitor_outputs)}</details><h2>Full source dossier</h2>${block('Final public state',d.public_final_state)}${block('Local p50 history',d.local_histories.p50)}${block('Local p100 history',d.local_histories.p100)}${block('Source links',d.source_links)}<h2>Human ground truth</h2><label>Label <select id="label"><option value="">Choose</option>${['POSITIVE','NEGATIVE','AMBIGUOUS'].map(x=>`<option ${sample.annotation?.human_label===x?'selected':''}>${x}</option>`).join('')}</select></label><button onclick="save()">Save & next sample</button> ${sample.next_pair_id?`<button onclick="location.href='/?pair_id='+encodeURIComponent(sample.next_pair_id)">Skip to next sample</button>`:''}</div>`}
async function save(){let label=document.querySelector('#label').value;let r=await fetch('/api/annotation',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({...sample.identity,human_label:label})});let v=await r.json();if(v.error){alert(v.error);return} location.href='/?pair_id='+encodeURIComponent(v.next_pair_id||sample.identity.pair_id)}load();</script>'''


def make_handler(review_dir: Path):
    class Handler(BaseHTTPRequestHandler):
        def _json(self, value: Any, status=200):
            data = json.dumps(value, ensure_ascii=False).encode(); self.send_response(status); self.send_header("Content-Type", "application/json; charset=utf-8"); self.send_header("Content-Length", str(len(data))); self.end_headers(); self.wfile.write(data)
        def do_GET(self):
            if self.path.split("?", 1)[0] == "/": self.send_response(200); self.send_header("Content-Type", "text/html; charset=utf-8"); self.end_headers(); self.wfile.write(HTML.encode()); return
            if self.path.split("?", 1)[0] != "/api/sample": self.send_error(404); return
            manifest = _read_csv(review_dir / MANIFEST_NAME); annotations = {r["pair_id"]: r for r in _read_csv(review_dir / ANNOTATIONS_NAME)}
            pair = parse_qs(urlparse(self.path).query).get("pair_id", [next((r["pair_id"] for r in manifest if r["pair_id"] not in annotations), manifest[0]["pair_id"])])[0]
            row = next((r for r in manifest if r["pair_id"] == pair), None)
            if not row: self._json({"error":"unknown sample"}, 404); return
            packet = _load_json(review_dir / row["packet_file"]); payload={"identity":packet["identity"],"blind_source_dossier":packet["blind_source_dossier"],"annotation":annotations.get(pair),"progress":{"done":len(annotations),"total":len(manifest)}}
            payload["next_pair_id"] = next((r["pair_id"] for r in manifest if r["pair_id"] not in annotations and r["pair_id"] != pair), None)
            payload["monitor_outputs"] = packet["revealed_monitor_outputs"]
            payload["evidence_maps"] = packet["exact_evidence_maps"]
            self._json(payload)
        def do_POST(self):
            if self.path != "/api/annotation": self.send_error(404); return
            try:
                length=int(self.headers.get("Content-Length","0")); saved=save_annotation(review_dir,json.loads(self.rfile.read(length))); manifest=_read_csv(review_dir/MANIFEST_NAME); annotations={r["pair_id"] for r in _read_csv(review_dir/ANNOTATIONS_NAME)}; next_pair=next((r["pair_id"] for r in manifest if r["pair_id"] not in annotations), None); self._json({"saved":saved,"next_pair_id":next_pair})
            except (ValueError, ValidationError, json.JSONDecodeError) as error: self._json({"error":str(error)},400)
        def log_message(self, *_args): pass
    return Handler


def main() -> None:
    root=Path(__file__).resolve().parents[3]; parser=argparse.ArgumentParser(description="Generate and serve V10 blinded human-review packets.")
    parser.add_argument("--data-dir",type=Path,default=root/"data/whistleblower"); parser.add_argument("--review-dir",type=Path,default=root/"data/whistleblower"/REVIEW_DIR_NAME); parser.add_argument("--rendered-dir",type=Path,default=root/"data/whistleblower/rendered_v10"); parser.add_argument("--run-exports-dir",type=Path,default=root/"game-of-agents/.goa_data/runs"); parser.add_argument("--generate-only",action="store_true"); parser.add_argument("--port",type=int,default=8765)
    args=parser.parse_args(); manifest=generate_packets(args.data_dir,args.review_dir,args.rendered_dir,args.run_exports_dir); print(f"Generated {len(manifest)} packets in {args.review_dir}")
    if not args.generate_only: print(f"Open http://127.0.0.1:{args.port}"); ThreadingHTTPServer(("127.0.0.1",args.port),make_handler(args.review_dir)).serve_forever()


if __name__ == "__main__": main()
