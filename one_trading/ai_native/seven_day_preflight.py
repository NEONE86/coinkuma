"""Preparation only. No collector, model, scheduler or order is activated here.

The required durable daily supervisor and continuous PAPER integration are not
implemented by this module. A successful disk check must never imply readiness.
"""
import argparse
import hashlib
import json
import shutil
from pathlib import Path

BASE_COMMIT='b6dcd93edc3e4dee812648b37591d6b183a95f02'
BASE_PROTOCOL_HASH='4fe0bdf2bb2208d54398fe411c1a89a9ba94a33c3c5d9d1df67ea91bdf67ab65'
PRIOR_DAILY_BYTES=1_415_878_705
DAY_BYTES=3_000_000_000
RAW_TOTAL_BYTES=7*DAY_BYTES
TOTAL_CAMPAIGN_BYTES=24_000_000_000
FREE_RESERVE_BYTES=5_000_000_000


def digest(value):
    return hashlib.sha256(json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False,allow_nan=False).encode()).hexdigest()


def frozen_contract(base):
    if digest(base)!=BASE_PROTOCOL_HASH:raise ValueError('PR26_PROTOCOL_CHANGED')
    return dict(version='seven-day-preparation-v1',base_commit=BASE_COMMIT,
        base_protocol_sha256=BASE_PROTOCOL_HASH,base_protocol=base,
        selected_strategies=['REVERSION','TREND','CASH'],selected_horizon_seconds=14400,
        selected_net_reward_risk=2,maximum_elapsed_seconds=604800,
        daily_max_bytes=DAY_BYTES,total_raw_max_bytes=RAW_TOTAL_BYTES,
        total_campaign_max_bytes=TOTAL_CAMPAIGN_BYTES,minimum_free_reserve_bytes=FREE_RESERVE_BYTES,
        expected_raw_bytes=7*PRIOR_DAILY_BYTES,expected_bytes_basis='ONE_ACTUAL_24H_NOT_GUARANTEED',
        paid_model_calls=0,authenticated_exchange_calls=0,real_orders=0,
        capacity_counterfactual_notionals=[100,500,1000],capacity_counts_as_strategy_pnl=False,
        maximum_period_extension_seconds=0,minimum_completed_nonoverlapping_trades=20,
        split_policy='NEW_COHORT_ALL_FORWARD_NO_MIDPOINT_RETRAIN_OR_RESET',
        activation_status='NOT_ACTIVATED',starts_at=None,deadline_at=None,
        runtime_status='NOT_IMPLEMENTED_OR_PROVEN_BY_THIS_PREPARATION_MODULE')


def assess(free_bytes):
    if type(free_bytes) is not int or free_bytes<0:raise ValueError('INVALID_FREE_BYTES')
    needed=TOTAL_CAMPAIGN_BYTES+FREE_RESERVE_BYTES
    return dict(status='BLOCKED_NOT_STARTED',storage_ok=free_bytes>=needed,
        measured_free_bytes=free_bytes,required_free_bytes=needed,
        additional_bytes_required=max(0,needed-free_bytes),
        expected_raw_bytes=7*PRIOR_DAILY_BYTES,
        blockers=['DURABLE_NONINTERACTIVE_EXECUTION_NOT_VERIFIED',
                  'DAILY_SUPERVISOR_AND_CONTINUOUS_PAPER_NOT_IMPLEMENTED_OR_VERIFIED'],
        collection_started=False,network_calls=0,paid_model_calls=0)


def counterfactual_depth(quote,notionals=(100,500,1000)):
    """Mechanical L1 size screen only; not a trade, return or fill claim.

    Callers must separately bind quote receipt chronology, age and spread. This
    function intentionally returns no PnL and cannot activate an order.
    """
    import math
    if any(type(quote.get(k)) not in (int,float) or not math.isfinite(quote[k]) or quote[k]<=0
           for k in ('ask','bid','ask_size','bid_size')) or quote['ask']<quote['bid']:
        raise ValueError('INVALID_L1')
    rows=[]
    for notional in notionals:
        if type(notional) not in (int,float) or not math.isfinite(notional) or notional<10:raise ValueError('INVALID_NOTIONAL')
        quantity=notional/(quote['ask']*1.0005)
        rows.append(dict(notional_usdt=notional,quantity=quantity,
            entry_l1_size_possible=quantity<=quote['ask_size']*.01,
            same_quote_exit_l1_size_possible=quantity<=quote['bid_size']*.01,
            classification='COUNTERFACTUAL_SIZE_ONLY_NOT_EXECUTION_OR_RETURN'))
    return rows


def main():
    p=argparse.ArgumentParser();p.add_argument('--volume',type=Path,required=True)
    p.add_argument('--output',type=Path,required=True);a=p.parse_args()
    repo=Path(__file__).resolve().parents[2];out=a.output.resolve()
    if out.is_relative_to(repo) or out.exists():raise ValueError('NEW_PRIVATE_OUTPUT_REQUIRED')
    base=json.loads((repo/'configs/opportunity_v1.json').read_bytes())
    report=assess(shutil.disk_usage(a.volume).free)
    out.mkdir(parents=False)
    (out/'frozen-contract.json').write_text(json.dumps(frozen_contract(base),indent=2))
    (out/'preflight.json').write_text(json.dumps(report,indent=2))
    print(json.dumps(report))

if __name__=='__main__':main()
