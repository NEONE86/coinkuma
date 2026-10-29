"""Offline review export verifier: standard library only; no strategy imports.

Usage: python verify_economic_batch_bundle.py /path/to/extracted/review
Verifies export hashes, actual raw bytes, timing, historical selection/costs,
unknown chain gate, original-record preservation and shared controller accounting.
Personal-path redactions are explicit derivatives, not original byte signatures.
"""
import hashlib,json,math,sys
from pathlib import Path
from datetime import datetime

def sha(b):return hashlib.sha256(b).hexdigest()
def canonical(x):return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
def instant(x):return datetime.fromisoformat(x.replace('Z','+00:00'))
def close(a,b):assert math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-8),(a,b)

def verify(root):
    root=Path(root);manifest=json.loads((root/'manifest.json').read_text('utf-8'))
    for rel,h in manifest.items():assert sha((root/rel).read_bytes())==h,rel
    ledgers={};redacted=0;unchanged=0
    for path in (root/'ledgers').glob('*.jsonl'):
        rows=[json.loads(x) for x in path.read_text('utf-8').splitlines()]
        assert len({(r['kind'],r['id']) for r in rows})==len(rows)
        for r in rows:
            assert sha(canonical(r['body']))==r['export_body_sha256']
            if r['redacted']:redacted+=1
            else:
                assert r['export_body_sha256']==r['original_body_sha256'];unchanged+=1
        ledgers[path.stem]=rows
    records=lambda name,kind:[r['body'] for r in ledgers[name] if r['kind']==kind]
    source=root/'source-current'/'one_trading';base=hashlib.sha256()
    files=sorted((source/'alpha_loop').rglob('*.py'))+sorted((source/'autonomy').glob('*.py'))+sorted((source/'economic_research').glob('*.py'))
    for path in files:base.update(path.relative_to(source).as_posix().encode());base.update(path.read_bytes())
    assert base.hexdigest()==records('alpha-valid','alpha_protocol')[0]['implementation_hash']
    for config in records('batch','batch_config'):
        version=root/'source-versions'/config['source_hash'];h=hashlib.sha256(base.hexdigest().encode())
        for name in ('batch_analysis.py','economic_batches.py'):h.update((version/name).read_bytes())
        assert h.hexdigest()==config['source_hash']
    batch=records('batch','batch_result')[0];trials=records('batch','trial_result')
    assert batch['attempted']==len(trials)==3 and batch['paper_entries']==0
    assert batch['counts']=={'REJECTED':2,'DATA_INSUFFICIENT':1,'NEXT_VALIDATION':0,'MORE_OBSERVATION':0,'EXECUTION_ERROR':0}
    # All exported response bodies remain original bytes, including HTTP errors.
    raw_count=0
    for name,rows in ledgers.items():
        for r in rows:
            if r['kind'] not in ('http_receipt','retrieval_receipt'):continue
            b=r['body'];assert instant(b['requested_at'])<=instant(b['received_at'])
            if b.get('sha256'):
                path=root/'raw'/name/(b['sha256']+'.bin')
                assert sha(path.read_bytes())==b['sha256'];raw_count+=1
    funding=next(r for r in trials if r['family']=='funding_crowding')
    data={}
    for symbol,receipts in funding['source_receipts'].items():
        bars,fees=[json.loads((root/'raw'/'batch'/(r['sha256']+'.bin')).read_bytes()) for r in receipts]
        data[symbol]={'bars':{r[0]:r for r in bars},'funding':fees}
        assert all(instant(r['received_at'])>instant(records('batch','research_batch')[0]['started_at']) for r in receipts)
    contexts=funding['contexts'];assert len(contexts)==13
    assert all(a['exit_ms']<b['entry_ms'] for a,b in zip(contexts,contexts[1:]))
    totals={arm:0. for arm in ('PRICE_ONLY','FUNDING_RULE','AI_RETROSPECTIVE','CASH')};trade_counts=dict.fromkeys(totals,0)
    selection=funding['selected_ai_rule']
    for c in contexts:
        assert c['entry_ms']==c['signal_ms']+7200000 and c['exit_ms']==c['entry_ms']+86400000
        for symbol,v in c['assets'].items():
            bars=data[symbol]['bars'];fs=sorted([f for f in data[symbol]['funding'] if f['fundingTime']<=c['signal_ms']],key=lambda f:f['fundingTime'])[-3:]
            close(v['past_24h_return'],float(bars[c['signal_ms']-3600000][4])/float(bars[c['signal_ms']-86400000][1])-1)
            close(v['last_three_funding_sum'],sum(float(f['fundingRate']) for f in fs))
            assert v['funding_source_time']==fs[-1]['fundingTime']<=c['signal_ms']
        ranked=sorted(c['assets'],key=lambda s:(c['assets'][s]['past_24h_return'],s))
        price=[s for s in ranked if c['assets'][s]['past_24h_return']<0]
        economic=[s for s in price if c['assets'][s]['last_three_funding_sum']<0]
        chosen={'PRICE_ONLY':price[:1],'FUNDING_RULE':economic[:1],'CASH':[],
                'AI_RETROSPECTIVE':price[:1] if selection=='PRICE_ONLY' else economic[:1] if selection=='FUNDING_RULE' else []}
        for arm,symbols in chosen.items():
            row=next(r for r in funding['rows'] if r['case_id']==c['id'] and r['arm']==arm)
            info=.1/13 if arm=='AI_RETROSPECTIVE' else 0;gross=cost=0
            if symbols:
                symbol=symbols[0];entry=float(data[symbol]['bars'][c['entry_ms']][1]);exit=float(data[symbol]['bars'][c['exit_ms']][1]);qty=1250/(entry*1.0016)
                gross=qty*(exit-entry);cost=qty*(entry+exit)*.0016;trade_counts[arm]+=1
                assert row['symbol']==symbol;close(row['entry_price'],entry);close(row['exit_price'],exit)
            else:assert row['symbol'] is None
            close(row['gross_usdt'],gross);close(row['trading_cost_usdt'],cost);close(row['information_cost_usdt'],info)
            close(row['net_usdt'],gross-cost-info);totals[arm]+=gross-cost-info
            assert row['not_a_fill'] and row['funding_cashflow']==0
    for arm,n in totals.items():
        close(funding['aggregates'][arm]['net_usdt'],n);assert funding['aggregates'][arm]['trades']==trade_counts[arm]
    models=records('batch','model_call');assert len(models)==2
    latencies=[]
    for call in models:
        assert call['status']=='CONNECTED_VALID' and not call['tools_enabled']
        modelroot=root/'models'/'batch'/call['id']
        payload=json.loads((modelroot/'input.json').read_text('utf-8'))
        assert sha(canonical(payload))==call['input_hash']
        assert sha(canonical(json.loads((modelroot/'answer.json').read_bytes())))==call['response_hash']
        assert sha((modelroot/'prompt.txt').read_bytes())==call['prompt_hash']
        latencies.append((instant(call['completed_at'])-instant(call['started_at'])).total_seconds())
        if call['id']==funding['model_call_id']:
            assert payload['earliest_context']==contexts[0] and 'cases' not in payload
            assert payload['cutoff']>records('batch','research_batch')[0]['started_at']
    credit=next(r for r in trials if r['family']=='credit_growth')
    assert credit['economic_gate']['state']=='UNKNOWN' and not credit['economic_gate']['allow_entry']
    failures=[r for r in records('batch','retrieval_receipt') if r.get('status')==403]
    assert failures and failures[0]['request_payload']['method']=='eth_getCode'
    growth=records('batch','growth_bridge_result');assert len(growth)==4
    assert all(r['fill'] is None and r['status']=='CASH' for r in growth)
    forced=next(r for r in trials if r['family']=='forced_flow')
    inputs=records('batch','batch_input');source=next(r['value'] for r in inputs if r['id'].endswith(':forced-prior'))
    for row in forced['checks']:
        snap=next(x for x in source['snapshots'] if x['id']==row['snapshot_id']);book=snap['books'][row['symbol']]
        close(row['gap'],snap['state'][row['symbol']]['reference_price']/book['asks'][0][0]-1)
        close(row['assumed_cost'],.003+book['spread_bps']/10000)
        assert row['above_cost']==(row['gap']>row['assumed_cost'])
    # The original ledger prefix is unchanged; originals stay local and exports
    # record both original and sanitized hashes. This is not external attestation.
    preservation=json.loads((root/'preservation.json').read_text('utf-8'))
    for name,prior in preservation['original_record_prefixes'].items():
        after={r['sequence']:r['original_body_sha256'] for r in ledgers[name]}
        assert all(after[int(seq)]==h for seq,h in prior.items())
    authority=json.loads((root/'authority.json').read_text('utf-8'))
    assert authority['alpha_registry']==preservation['alpha_registry_before']
    for prior in preservation['original_tasks']:
        assert next(t for t in authority['tasks'] if t['task_key']==prior['task_key'])==prior
    for engine in authority['engine_budgets']:
        tasks=[t for t in authority['tasks'] if next(e for e in authority['experiments'] if e['id']==t['experiment_id'])['engine']==engine['engine']]
        assert engine['cash_spent']==sum(t['actual_cash'] or 0 for t in tasks if t['state'] in ('DONE','FAILED'))
        assert engine['time_spent']==sum(t['actual_time'] or 0 for t in tasks if t['state'] in ('DONE','FAILED'))
        assert engine['time_reserved']==sum(json.loads(t['task_json'])['time_seconds'] for t in tasks if t['state']=='CLAIMED')
    for receipt in records('batch','trial_receipt'):
        task=next(t for t in authority['tasks'] if t['task_key']==receipt['id'])
        assert task['actual_time']==receipt['elapsed_seconds'];assert json.loads(task['result_json'])==receipt['result']
    d=records('batch','design_diagnostic')[0];assert d['threshold']==120 and d['planned_maximum_nonoverlapping_units']==56
    for effect in d['effects']:
        close(effect['incremental_e_at_planned_n'],sum((1+l*effect['capital_net_effect']/.05/2)**56 for l in (.1,.25,.5,.75))/4)
    assert batch['new_independent_matured_forward_results']==0 and not batch['edge_proven']
    return dict(status='PASS',manifest_files=len(manifest),unchanged_export_records=unchanged,redacted_derivative_records=redacted,
                raw_receipts_verified=raw_count,historical_units=13,counterfactual_net_usdt=totals,
                historical_trade_counts=trade_counts,actual_new_paper_entries=0,model_seconds=latencies,
                existing_records_preserved=True,global_alpha_unchanged=True,forward_edge_proven=False,
                scope='First economic batch export only. No market edge, external timestamp attestation, mature GROWTH return or historical executable-price claim.')

if __name__=='__main__':print(json.dumps(verify(sys.argv[1]),ensure_ascii=False,indent=2))
