"""V9: temporal V5 whistleblower and post-hoc V8 Sentinel over V6 successes."""
from __future__ import annotations

import argparse
import csv
import json
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from decimal import Decimal, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Callable

try:
    from src.scripts.phase_7.ollama_marketplace_pre_action_audit import PRE_ACTION_INSTRUCTIONS
    from src.scripts.phase_7.ollama_marketplace_pre_action_sanitized import build_sanitized_request
    from src.scripts.phase_7.ollama_marketplace_sentinel_final_state import build_final_state_request
    from src.scripts.phase_7.ollama_marketplace_terminal_discovery import _load_records
    from src.scripts.phase_7.ollama_whistleblower_audit import (
        DEFAULT_MODEL, OLLAMA_CHAT_URL, RESULT_SCHEMA_VERSION, AuditItem, MLflowLogger,
        ValidationError, _acquire_run_lock, _append_result, load_dotenv_key, ollama_chat,
        parse_response,
    )
except ModuleNotFoundError as error:
    if error.name != "src": raise
    from ollama_marketplace_pre_action_audit import PRE_ACTION_INSTRUCTIONS
    from ollama_marketplace_pre_action_sanitized import build_sanitized_request
    from ollama_marketplace_sentinel_final_state import build_final_state_request
    from ollama_marketplace_terminal_discovery import _load_records
    from ollama_whistleblower_audit import DEFAULT_MODEL, OLLAMA_CHAT_URL, RESULT_SCHEMA_VERSION, AuditItem, MLflowLogger, ValidationError, _acquire_run_lock, _append_result, load_dotenv_key, ollama_chat, parse_response

PROMPT_VERSION_LOCAL = "phase7-marketplace-v9-temporal-v5-sanitized"
PROMPT_VERSION_SENTINEL = "phase7-marketplace-v9-final-state-sentinel"
EXPERIMENT_LOCAL = "phase7_ollama_marketplace_v9_temporal_v5_sanitized"
EXPERIMENT_SENTINEL = "phase7_ollama_marketplace_v9_final_state_sentinel"
V6_RESULTS = "audit_marketplace_terminal_v6_discovery_sanitized_combined.jsonl"
V6_MANIFEST = "phase7_terminal_discovery_manifest.csv"
PROGRESSES = (("p25", Decimal("0.25")), ("p50", Decimal("0.50")), ("p75", Decimal("0.75")), ("p100", Decimal("1.00")))
MANIFEST_FIELDS = ("v6_checkpoint_id", "run_id", "agent_id", "trajectory_file", "step_count", "source_terminal_step_id", "source_terminal_timestamp", "p25_index", "p50_index", "p75_index", "p100_index")


def _read_csv(path: Path) -> list[dict[str, str]]:
    if not path.is_file(): raise ValidationError(f"missing required artifact: {path}")
    with path.open(newline="", encoding="utf-8") as handle: return list(csv.DictReader(handle))


def _read_jsonl(path: Path) -> list[dict[str, Any]]:
    if not path.is_file(): raise ValidationError(f"missing required artifact: {path}")
    rows=[]
    for number, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
        try: rows.append(json.loads(line))
        except json.JSONDecodeError as error: raise ValidationError(f"malformed JSONL at {path}:{number}") from error
    return rows


def _round_index(progress: Decimal, step_count: int) -> int:
    return int((progress * Decimal(step_count - 1)).quantize(Decimal("1"), rounding=ROUND_HALF_UP))


def _expected_manifest(data_dir: Path) -> list[dict[str, str]]:
    completed = [row for row in _read_jsonl(data_dir / V6_RESULTS) if row.get("status") == "completed"]
    if len(completed) != 25: raise ValidationError(f"expected 25 successful V6 rows, found {len(completed)}")
    source = {(row["run_id"], row["agent_id"]): row for row in _read_csv(data_dir / V6_MANIFEST)}
    rows=[]
    for result in sorted(completed, key=lambda row: row["checkpoint_id"]):
        key=(str(result["run_id"]), str(result["agent_id"])); original=source.get(key)
        if not original: raise ValidationError(f"V6 successful row lacks manifest identity: {key}")
        count=int(original["step_count"])
        if int(result["decision_step_index"]) != count-1: raise ValidationError(f"V6 source is not terminal: {result['checkpoint_id']}")
        indexes={name: _round_index(progress,count) for name,progress in PROGRESSES}
        if len(set(indexes.values())) != 4: raise ValidationError(f"V9 checkpoint indexes are not distinct for {key}")
        rows.append({"v6_checkpoint_id":str(result["checkpoint_id"]), "run_id":key[0], "agent_id":key[1], "trajectory_file":original["trajectory_file"], "step_count":str(count), "source_terminal_step_id":str(result["step_id"]), "source_terminal_timestamp":str(result["timestamp"]), **{f"{name}_index":str(index) for name,index in indexes.items()}})
    return rows


def freeze_manifest(data_dir: Path, manifest_path: Path) -> list[dict[str, str]]:
    expected=_expected_manifest(data_dir)
    if manifest_path.exists():
        actual=_read_csv(manifest_path)
        if actual != expected: raise ValidationError(f"existing V9 manifest differs from completed V6 cohort: {manifest_path}")
        return actual
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    with manifest_path.open("w", newline="", encoding="utf-8") as handle:
        writer=csv.DictWriter(handle, fieldnames=MANIFEST_FIELDS); writer.writeheader(); writer.writerows(expected)
    return expected


def load_v9_items(data_dir: Path, manifest_path: Path) -> tuple[list[AuditItem], list[AuditItem]]:
    rows=freeze_manifest(data_dir, manifest_path)
    if len(rows)!=25 or len({(r["run_id"],r["agent_id"]) for r in rows})!=25: raise ValidationError("V9 manifest must contain 25 unique successful V6 trajectories")
    local: list[AuditItem]=[]; sentinel: list[AuditItem]=[]; cache: dict[str,list[dict[str,Any]]]={}
    for ordinal,row in enumerate(rows,1):
        records=_load_records(data_dir,row,cache); count=len(records)
        if count != int(row["step_count"]): raise ValidationError("V9 step count disagrees with records")
        for name,_ in PROGRESSES:
            index=int(row[f"{name}_index"]); record=records[index]
            if int(record["decision_step_index"]) != index: raise ValidationError("V9 local checkpoint index mismatch")
            checkpoint={"checkpoint_id":f"v9_local_{ordinal:03d}_{name}", "pair_id":f"v9_case_{ordinal:03d}", "cohort":"v9_temporal_local", "checkpoint_name":name, "run_id":row["run_id"], "agent_id":row["agent_id"], "decision_step_index":str(index), "step_id":str(record["step_id"]), "timestamp":str(record["timestamp"]), "source_v6_checkpoint_id":row["v6_checkpoint_id"]}
            local.append(AuditItem(checkpoint,{"trajectory_file":row["trajectory_file"]},record))
        terminal=records[-1]
        checkpoint={"checkpoint_id":f"v9_sentinel_{ordinal:03d}_final", "pair_id":f"v9_case_{ordinal:03d}", "cohort":"v9_final_state_sentinel", "checkpoint_name":"final", "run_id":row["run_id"], "agent_id":row["agent_id"], "decision_step_index":str(terminal["decision_step_index"]), "step_id":str(terminal["step_id"]), "timestamp":str(terminal["timestamp"]), "source_v6_checkpoint_id":row["v6_checkpoint_id"]}
        sentinel.append(AuditItem(checkpoint,{"trajectory_file":row["trajectory_file"]},terminal))
    return local,sentinel


def build_local_request(item: AuditItem, model: str) -> tuple[dict[str,Any],dict[str,Any]]:
    request,summary=build_sanitized_request(item,model,PRE_ACTION_INSTRUCTIONS)
    context=json.loads(request["messages"][1]["content"])
    context["context_policy"]="selected_agent_prior_non_prompt_history_only"
    request["messages"][1]["content"]=json.dumps(context,ensure_ascii=False,separators=(",",":"))
    forbidden={"current_prompt","run_config","model_output","tool_calls","environment_events","labels","taxonomy"}
    if forbidden & set(context) or any(x.get("kind")=="prompt" for x in context["prior_transcript_blocks"]): raise ValidationError("V9 local request violates V5 boundary")
    return request,summary


def _latest_results(path: Path) -> dict[str,dict[str,Any]]:
    return {str(row["checkpoint_id"]):row for row in (_read_jsonl(path) if path.exists() else [])}


def _result(item: AuditItem, condition: str, model: str, timeout: float, retries: int, raw: str|None, meta: dict[str,Any], elapsed: float, status: str, error: str|None, summary: dict[str,Any], attempt: int) -> dict[str,Any]:
    parsed=(parse_response(raw) if raw is not None else {"valid":False,"kind":None,"report":None,"parse_error":"provider_failure"}) if condition=="sentinel" else {"valid":raw is not None,"kind":"free_form_diagnostic" if raw is not None else None,"report":None,"parse_error":None if raw is not None else "provider_failure"}
    prompt= PROMPT_VERSION_SENTINEL if condition=="sentinel" else PROMPT_VERSION_LOCAL
    policy= "post_hoc_final_run_state" if condition=="sentinel" else "before_selected_outer_decision_step"
    return {"schema_version":RESULT_SCHEMA_VERSION,"checkpoint_id":item.checkpoint["checkpoint_id"],"checkpoint_name":item.checkpoint["checkpoint_name"],"pair_id":item.checkpoint["pair_id"],"cohort":item.checkpoint["cohort"],"run_id":item.checkpoint["run_id"],"agent_id":item.checkpoint["agent_id"],"decision_step_index":int(item.checkpoint["decision_step_index"]),"step_id":item.checkpoint["step_id"],"timestamp":item.checkpoint["timestamp"],"source_v6_checkpoint_id":item.checkpoint["source_v6_checkpoint_id"],"attempt":attempt,"request_config":{"provider":"ollama_cloud","url":OLLAMA_CHAT_URL,"model":model,"prompt_version":prompt,"stream":False,"timeout_seconds":timeout,"retries":retries,"context_boundary":policy},"sanitation_summary":summary,"raw_response":raw,"parse_result":parsed,"report_warranted":parsed.get("kind")=="report","provider_metadata":meta,"elapsed_seconds":elapsed,"status":status,"error":error}


def _run_condition(items: list[AuditItem], condition: str, results_path: Path, rendered_dir: Path, data_dir: Path, run_exports_dir: Path, *, model: str, api_key: str|None, timeout: float, retries: int, parallelism: int, validate_only: bool, only_ids: set[str], client: Callable[[dict[str,Any],str,float,int],tuple[str,dict[str,Any]]], logger: MLflowLogger|None) -> dict[str,int]:
    # Validate/materialize every request before any provider call. Store paths rather than keeping 125 large prompts in memory.
    rendered_dir.mkdir(parents=True,exist_ok=True); materialized: dict[str,tuple[Path,dict[str,Any]]]={}
    for item in items:
        request,summary=(build_final_state_request(item,data_dir,run_exports_dir,model) if condition=="sentinel" else build_local_request(item,model))
        path=rendered_dir/f"{item.checkpoint['checkpoint_id']}.json"; path.write_text(json.dumps(request,ensure_ascii=False,indent=2),encoding="utf-8"); materialized[item.checkpoint["checkpoint_id"]]=(path,summary)
    if parallelism < 1: raise ValidationError("parallelism must be at least one")
    if validate_only:return {"validated":len(items),"skipped":0,"written":0}
    if not api_key:raise RuntimeError("OLLAMA_API_KEY is required for V9 live execution")
    existing=_latest_results(results_path)
    pending=[item for item in items if (not only_ids or item.checkpoint["checkpoint_id"] in only_ids) and existing.get(item.checkpoint["checkpoint_id"],{}).get("status")!="completed"]
    lock=_acquire_run_lock(results_path)
    try:
        active_logger=logger or MLflowLogger(os.environ.get("MLFLOW_TRACKING_URI",f"sqlite:///{(data_dir/'mlflow.db').resolve()}"),EXPERIMENT_SENTINEL if condition=="sentinel" else EXPERIMENT_LOCAL)
        active_logger.start_parent(f"phase7-v9-{condition}",{"model":model,"prompt_version":PROMPT_VERSION_SENTINEL if condition=="sentinel" else PROMPT_VERSION_LOCAL,"checkpoint_count":str(len(pending)),"parallelism":str(parallelism)})
        try:
            def call(item:AuditItem):
                request=json.loads(materialized[item.checkpoint["checkpoint_id"]][0].read_text(encoding="utf-8")); summary=materialized[item.checkpoint["checkpoint_id"]][1]; start=time.monotonic(); old=existing.get(item.checkpoint["checkpoint_id"],{}); attempt=int(old.get("attempt",0))+1
                try: raw,meta=client(request,api_key,timeout,retries); return _result(item,condition,model,timeout,retries,raw,meta,time.monotonic()-start,"completed",None,summary,attempt),request
                except Exception as exc:return _result(item,condition,model,timeout,retries,None,{},time.monotonic()-start,"request_failed",str(exc),summary,attempt),request
            with ThreadPoolExecutor(max_workers=parallelism,thread_name_prefix=f"phase7-v9-{condition}") as pool:
                futures=[pool.submit(call,item) for item in pending]
                for future in as_completed(futures):
                    result,request=future.result(); _append_result(results_path,result); active_logger.log_checkpoint(result,request)
        finally: active_logger.close()
        return {"validated":len(items),"skipped":len(items)-len(pending),"written":len(pending)}
    finally:lock.unlink(missing_ok=True)


def run_v9(data_dir:Path, *, manifest_path:Path, local_results:Path, sentinel_results:Path, local_rendered:Path, sentinel_rendered:Path, run_exports_dir:Path, condition:str="both", model:str=DEFAULT_MODEL, api_key:str|None=None, timeout:float=300,retries:int=2,parallelism:int=10,validate_only:bool=False,only_ids:set[str]|None=None,client:Callable[[dict[str,Any],str,float,int],tuple[str,dict[str,Any]]]=ollama_chat,logger_factory:Callable[[str],MLflowLogger]|None=None)->dict[str,dict[str,int]]:
    local,sentinel=load_v9_items(data_dir,manifest_path); only_ids=only_ids or set(); result={}
    if condition in {"both","local"}:result["local"]=_run_condition(local,"local",local_results,local_rendered,data_dir,run_exports_dir,model=model,api_key=api_key,timeout=timeout,retries=retries,parallelism=parallelism,validate_only=validate_only,only_ids=only_ids,client=client,logger=logger_factory("local") if logger_factory else None)
    if condition in {"both","sentinel"}:result["sentinel"]=_run_condition(sentinel,"sentinel",sentinel_results,sentinel_rendered,data_dir,run_exports_dir,model=model,api_key=api_key,timeout=timeout,retries=retries,parallelism=parallelism,validate_only=validate_only,only_ids=only_ids,client=client,logger=logger_factory("sentinel") if logger_factory else None)
    return result


def main()->None:
    root=Path(__file__).resolve().parents[3]; p=argparse.ArgumentParser(description="Run resumable V9 temporal local and Sentinel audits.")
    p.add_argument("--data-dir",type=Path,default=root/"data/whistleblower");p.add_argument("--manifest",type=Path,default=root/"data/whistleblower/phase7_v9_temporal_manifest.csv");p.add_argument("--local-results",type=Path,default=root/"data/whistleblower/audit_marketplace_v9_temporal_local.jsonl");p.add_argument("--sentinel-results",type=Path,default=root/"data/whistleblower/audit_marketplace_v9_final_state_sentinel.jsonl");p.add_argument("--local-rendered",type=Path,default=root/"data/whistleblower/rendered_v9_temporal_local");p.add_argument("--sentinel-rendered",type=Path,default=root/"data/whistleblower/rendered_v9_final_state_sentinel");p.add_argument("--run-exports-dir",type=Path,default=root/"game-of-agents/.goa_data/runs");p.add_argument("--condition",choices=("both","local","sentinel"),default="both");p.add_argument("--only",default="");p.add_argument("--model",default=DEFAULT_MODEL);p.add_argument("--timeout",type=float,default=300);p.add_argument("--retries",type=int,default=2);p.add_argument("--parallelism",type=int,default=10);p.add_argument("--validate-only",action="store_true")
    a=p.parse_args();key=None if a.validate_only else load_dotenv_key(root/".env");only={x for x in a.only.split(",") if x}
    print(json.dumps(run_v9(a.data_dir,manifest_path=a.manifest,local_results=a.local_results,sentinel_results=a.sentinel_results,local_rendered=a.local_rendered,sentinel_rendered=a.sentinel_rendered,run_exports_dir=a.run_exports_dir,condition=a.condition,model=a.model,api_key=key,timeout=a.timeout,retries=a.retries,parallelism=a.parallelism,validate_only=a.validate_only,only_ids=only),sort_keys=True))

if __name__=="__main__":main()
