"""Check shared-engine default behavior against preserved PR3 actual results."""
import argparse
import math
from pathlib import Path
import sys
import json

ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from one_trading.historical_data import load_histories,ms,write_json
from one_trading.walkforward import HistoricalPanel
from one_trading.cashflow_alpha import default_config,run


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    p.add_argument('--archive',required=True);p.add_argument('--output',required=True)
    a=p.parse_args();h,_=load_histories(a.archive);config=default_config();panel=HistoricalPanel(h,config)
    old=json.loads((ROOT/'docs/experiments/2026-10-25/comparison.json').read_text(encoding='utf-8'))
    checks={}
    keys=['total_return','cagr','max_drawdown','profit_factor','trade_count','win_rate','average_daily_exposure','fees_paid']
    for case,fee,slip in [('base',.001,.001),('stress',.002,.003)]:
        result=run(panel,{**config,'fee_rate':fee,'slippage_rate':slip},ms('2019-01-01'),ms('2026-10-01'))
        expected=old[case]['continuous'];expected=expected.get('summary',expected)
        for k in keys:
            if not math.isclose(result['summary'][k],expected[k],rel_tol=1e-9,abs_tol=1e-9):
                raise AssertionError('preserved_v1_actual_result_changed:'+case+':'+k)
        checks[case]={k:result['summary'][k] for k in keys}
    value=dict(preserved_pr3_actual_results_match=True,metrics=checks,
               interpretation='Shared-engine regression only; no new strategy variant and no edits to old results.')
    write_json(a.output,value);print(json.dumps(value),flush=True)
