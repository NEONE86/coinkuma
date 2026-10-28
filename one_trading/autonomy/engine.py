"""Resumable one-hour experiment with separately idempotent maintenance."""
import os,uuid
from datetime import timedelta
from .store import Store,utc,instant,digest,process_lock,require_disk
from .market import collect
from .validation import validate_packet,source_identity
from .model import invoke,resolved_memory
from .policies import evaluate,register
from .paper import maintain
from .outcomes import mature,compare
from .growth import maintain_growth
from .reporting import scheduler_status,diagnostics

def hour_id():
    return instant(utc()).replace(minute=0,second=0,microsecond=0).isoformat()

def tick(root,config,stop_after=None):
    with process_lock(root):
        store=Store(root)
        run_id=uuid.uuid4().hex;start=utc();hour=hour_id()
        try:
            require_disk(root);store.frozen_config(config)
            deployment=store.get("deployment","sealed",True)
            if deployment and deployment["source_hash"]!=source_identity():raise ValueError("SOURCE_CHANGED_AFTER_SEAL_NEW_WORKSPACE_REQUIRED")
            previous_stage=store.stage(hour)
            store.append("run_start",run_id,dict(id=run_id,started_at=start,pid=os.getpid(),
                         hour=hour,previous_stage=previous_stage,actual_wall_clock=True,
                         origin="WINDOWS_SCHEDULER" if os.environ.get("ONE_AUTONOMY_SCHEDULED")=="1" else "CLI",
                         source_hash=source_identity()))
            packet=store.get("market_packet",hour,True)
            if packet is None:
                packet=store.append("market_packet",hour,collect(store,config,hour))
                store.checkpoint(hour,"COLLECTED")
            validate_packet(store,config,packet)
            if stop_after=="packet":
                store.append("planned_interruption",run_id,dict(id=run_id,hour=hour,stage="COLLECTED",at=utc()))
                return dict(status="INTERRUPTED_AT_DURABLE_CHECKPOINT",hour=hour,run_id=run_id)
            registration=store.get("registration",hour,True)
            if registration is None:
                memory=resolved_memory(store,packet["cutoff"],packet)
                payload=dict(packet=packet,memory=memory,config={k:config[k] for k in (
                        "signal_markout_cost_per_side","stress_cost_per_side","risk_fraction_per_position",
                        "horizons_hours","base_residual_threshold","numeric_oi_quantity_threshold")},
                        memory_status="NO_MATURED_MEMORY" if not memory else "RESOLVED_BEFORE_CUTOFF_ONLY")
                call=invoke(store,config,digest(["cf-model",hour]),payload)
                store.checkpoint(hour,"MODEL_RECORDED")
                registration=register(store,config,packet,evaluate(config,packet,call))
                store.checkpoint(hour,"REGISTERED")
            if stop_after=="registration":
                store.append("planned_interruption",run_id,dict(id=run_id,hour=hour,stage="REGISTERED",at=utc()))
                return dict(status="INTERRUPTED_AT_DURABLE_CHECKPOINT",hour=hour,run_id=run_id)
            accounts=maintain(store,config);mature(store)
            try:maintain_growth(store)
            except Exception as exc:store.failure("growth_isolated",exc)
            comparison=compare(store,config)
            next_due=(instant(hour)+timedelta(hours=1,minutes=2)).isoformat()
            report=dict(id=run_id,hour=hour,started_at=start,completed_at=utc(),
                  execution_type="REPLAY_MAINTENANCE" if previous_stage=="COMPLETE" else "INDEPENDENT_HOURLY_EXECUTION",
                  resumed=previous_stage is not None and previous_stage!="COMPLETE",
                  packet_hash=digest(packet),cutoff=packet["cutoff"],registration_ids=registration["decision_ids"],
                  context_hash=registration["context_hash"],accounts=accounts,comparison=comparison,
                  next_due=next_due,live_trading_enabled=False)
            store.append("run_complete",run_id,report)
            store.append("next_experiment",run_id,dict(id=run_id,next_due=next_due,
                     frozen_config_hash=digest(config),action="COLLECT_NEXT_INDEPENDENT_HOUR",
                     promotion_allowed=False,reason="Accumulate prospective resolved counterfactuals before any rule change"))
            store.checkpoint(hour,"COMPLETE")
            return report
        except Exception as exc:
            try:store.failure("tick",exc,run_id=run_id,hour=hour)
            except Exception:pass
            raise
        finally:store.close()

def status(root,config):
    store=Store(root)
    try:
        store.verify()
        runs=store.all("run_complete");unique={r["hour"] for r in runs}
        scheduler=scheduler_status(store.root)
        return dict(PRODUCT_READY=bool(runs),FORWARD_RUNNING=scheduler["active"],scheduler=scheduler,diagnostics=diagnostics(store,config),
                    EDGE_PROVEN=False,LIVE_TRADING_ENABLED=False,
                    independent_hours=len(unique),runs=len(runs),
                    registrations=len(store.all("registration")),
                    actual_model_calls={s:sum(r["status"]==s for r in store.all("model_call"))
                                        for s in ("CONNECTED_VALID","MODEL_NOT_CONNECTED","MODEL_INVALID_OUTPUT","MODEL_TIMEOUT")},
                    paper_entries=len(store.all("paper_entry")),paper_exits=len(store.all("paper_exit")),
                    growth_experiments=len(store.all("growth_experiment")),comparison=compare(store,config))
    finally:store.close()
