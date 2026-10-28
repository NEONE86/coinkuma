"""Actual bounded Codex subprocess; no order, shell, web or MCP tools."""
import json, shutil, subprocess, uuid
from pathlib import Path
from .store import utc, instant, digest, canonical, atomic_bytes
from .market import number

SCHEMA={"type":"object","properties":{"assets":{"type":"array","items":{
 "type":"object","properties":{
  "symbol":{"type":"string"},"classification":{"type":"string","enum":["FORCED_REDUCTION_HYPOTHESIS","TREND","NEUTRAL","UNKNOWN"]},
  "action":{"type":"string","enum":["LONG","NO_TRADE"]},
  "p_excess_6h":{"type":"number","minimum":0,"maximum":1},
  "p_excess_24h":{"type":"number","minimum":0,"maximum":1},
  "p_excess_72h":{"type":"number","minimum":0,"maximum":1},
  "rationale":{"type":"string"},"counter_hypothesis":{"type":"string"}},
 "required":["symbol","classification","action","p_excess_6h","p_excess_24h","p_excess_72h","rationale","counter_hypothesis"],
 "additionalProperties":False}}},"required":["assets"],"additionalProperties":False}
SYSTEM="""You are a closed-input prospective research classifier. Use ONLY the supplied frozen observations and already resolved memory. Do not use external knowledge, tools, web, files or future prices. OI notional decline is NOT proof of liquidations. Quantity decline is compatible with forced reduction but also voluntary closing; explicitly retain the alternative. Missing observations are missing. Predict probability that each asset's future long signal return after the common assumed costs exceeds the equal-weight other-asset benchmark at 6/24/72h. This is a signal markout, not a guaranteed fill or profit. LONG only if all numeric inputs are valid and evidence supports a conditional relative rebound; otherwise NO_TRADE. Return exactly the requested schema and universe. Rationale and counter-hypothesis must distinguish observed inputs from inference. Never place orders."""

def validate_answer(answer,universe):
    if not isinstance(answer,dict) or set(answer)!={"assets"}:raise ValueError("model_schema")
    rows=answer["assets"]
    if not isinstance(rows,list) or len(rows)!=len(universe):raise ValueError("model_universe")
    expected=set(SCHEMA["properties"]["assets"]["items"]["required"])
    if any(not isinstance(r,dict) or set(r)!=expected for r in rows):raise ValueError("model_fields")
    if sorted(r["symbol"] for r in rows)!=sorted(universe):raise ValueError("model_asset_identity")
    for r in rows:
        if r["classification"] not in SCHEMA["properties"]["assets"]["items"]["properties"]["classification"]["enum"]:raise ValueError("model_classification")
        if r["action"] not in ("LONG","NO_TRADE"):raise ValueError("model_action")
        for h in (6,24,72):
            p=r["p_excess_"+str(h)+"h"]
            if not isinstance(p,(int,float)) or isinstance(p,bool) or not 0<=number(p)<=1:raise ValueError("model_probability")
        for name in ("rationale","counter_hypothesis"):
            if not isinstance(r[name],str) or not 1<=len(r[name])<=4000:raise ValueError("model_rationale")
    return answer

def resolved_memory(store,cutoff,packet=None):
    rows=[]
    decisions=store.all("policy_decision")
    for r in store.all("outcome"):
        if r["status"]!="RESOLVED" or instant(r["stored_at"])>instant(cutoff) or instant(r["resolved_at"])>instant(cutoff):continue
        q=store.get("question",r["question_id"],True)
        if not q or r["horizon_hours"]!=72 or type(r["label"]) is not bool:continue
        if q["symbol"]!=r["symbol"] or q["horizon_hours"]!=72:raise ValueError("memory_outcome_identity")
        ds=[d for d in decisions if r["question_id"] in d["question_ids"]]
        if not ds:continue
        origin=store.get("market_packet",ds[0]["hour"],True)
        if not origin or instant(origin["cutoff"])>instant(q["registered_at"]):continue
        features={k:origin["assets"][r["symbol"]].get(k) for k in
                  ("residual_1h","oi_quantity_change","last_settled_funding_per_hour","basis","taker_buy_fraction")}
        distance=0.
        current=packet["assets"].get(r["symbol"],{}) if packet else {}
        scales={"residual_1h":.01,"oi_quantity_change":.01,"last_settled_funding_per_hour":.0001,"basis":.001,"taker_buy_fraction":.1}
        for k,v in features.items():
            if v is None or current.get(k) is None:distance+=10
            else:distance+=min(100,abs(v-current[k])/scales[k])
        row={k:r[k] for k in ("question_id","symbol","horizon_hours","excess_return","label","resolved_at")}
        row.update(registered_at=q["registered_at"],deadline=q["deadline"],historical_features=features,
                   original_predictions=[dict(policy=d["policy"],probability=d["probabilities"]["72"],action=d["action"],reason=d["reason"]) for d in ds],
                   similarity_distance=distance,available_at=r["stored_at"])
        rows.append(row)
    independent=[];last_end={}
    for r in sorted(rows,key=lambda x:x["registered_at"]):
        if r["symbol"] in last_end and instant(r["registered_at"])<last_end[r["symbol"]]:continue
        independent.append(r);last_end[r["symbol"]]=instant(r["deadline"])
    return sorted(independent,key=lambda x:(x["similarity_distance"],x["registered_at"]))[:20]

def invoke(store,config,key,payload,schema=SCHEMA,instruction=SYSTEM,validator=None):
    old=store.get("model_call",key,optional=True)
    if old:return old
    started=utc();folder=store.root/"models"/key/uuid.uuid4().hex;folder.mkdir(parents=True,exist_ok=True)
    prompt=instruction+"\nFROZEN_INPUT:\n"+canonical(payload).decode()
    atomic_bytes(folder/"input.json",canonical(payload))
    atomic_bytes(folder/"schema.json",canonical(schema))
    atomic_bytes(folder/"prompt.txt",prompt.encode())
    executable=shutil.which(config["model_executable"])
    status="MODEL_NOT_CONNECTED";answer=None;error=None;code=None;stdout="";stderr=""
    command=[]
    if config["model_backend"]=="codex" and executable:
        command=[executable,"exec","--ignore-user-config","--ephemeral","--skip-git-repo-check",
                 "--sandbox","read-only","--model",config["model_name"],
                 "-c","features.shell_tool=false","-c",'web_search="disabled"',
                 "--output-schema",str(folder/"schema.json"),"--output-last-message",str(folder/"answer.json"),"--json","-"]
        try:
            run=subprocess.run(command,input=prompt,text=True,encoding="utf-8",errors="replace",
                 stdout=subprocess.PIPE,stderr=subprocess.PIPE,cwd=folder,
                 timeout=config["model_timeout_seconds"],creationflags=getattr(subprocess,"CREATE_NO_WINDOW",0))
            stdout=run.stdout;stderr=run.stderr;code=run.returncode
            if run.returncode:raise RuntimeError("model_exit_"+str(run.returncode))
            for line in stdout.splitlines():
                event=json.loads(line)
                if event.get("type") in ("item.started","item.completed") and event.get("item",{}).get("type") not in ("agent_message","reasoning"):
                    raise ValueError("unexpected_model_tool_use")
            answer=json.loads((folder/"answer.json").read_text(encoding="utf-8"))
            (validator or (lambda a:validate_answer(a,payload["packet"]["universe"])))(answer)
            status="CONNECTED_VALID"
        except subprocess.TimeoutExpired as exc:
            stdout=exc.stdout.decode(errors="replace") if isinstance(exc.stdout,bytes) else (exc.stdout or "")
            stderr=exc.stderr.decode(errors="replace") if isinstance(exc.stderr,bytes) else (exc.stderr or "")
            status="MODEL_TIMEOUT";error="bounded_model_timeout"
        except Exception as exc:
            status="MODEL_INVALID_OUTPUT" if isinstance(exc,(ValueError,KeyError,TypeError)) else "MODEL_NOT_CONNECTED"
            error=str(exc)[:400];answer=None
    else:error="configured_backend_unavailable"
    atomic_bytes(folder/"stdout.jsonl",stdout.encode());atomic_bytes(folder/"stderr.txt",stderr.encode())
    record=dict(id=key,status=status,model=config["model_name"],backend=config["model_backend"],
                prompt_version=config["model_prompt_version"],input_hash=digest(payload),
                prompt_hash=__import__("hashlib").sha256(prompt.encode()).hexdigest(),
                response_hash=digest(answer) if answer else None,answer=answer,
                stdout_hash=__import__("hashlib").sha256(stdout.encode()).hexdigest(),
                stderr_hash=__import__("hashlib").sha256(stderr.encode()).hexdigest(),
                artifact_directory=str(folder.relative_to(store.root)),
                started_at=started,completed_at=utc(),returncode=code,error=error,
                command=command,tools_enabled=False,closed_input_instruction=True)
    atomic_bytes(folder/"completed.json",canonical(record))
    return store.append("model_call",key,record)
