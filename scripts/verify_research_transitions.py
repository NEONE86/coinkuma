"""Offline semantic audit of any typed-hypothesis batch; standard library only.

No strategy import, no network, no execution of model-provided instructions.
Usage: python verify_research_transitions.py EXTRACTED_BUNDLE/latest
The separately supplied first-batch verifier retains its first-batch-only scope.
"""
import hashlib,json,math,sys
from pathlib import Path
from datetime import datetime

def canonical(x):return json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()
def sha(b):return hashlib.sha256(b).hexdigest()
def digest(x):return sha(canonical(x))
def instant(x):return datetime.fromisoformat(x.replace('Z','+00:00'))
def close(a,b):assert type(a) in (float,int) and math.isfinite(a) and math.isclose(a,b,rel_tol=1e-10,abs_tol=1e-8),(a,b)
ABS='SPOT_SELL_PRESSURE_ABSORPTION';LAG='CROSS_ASSET_INFORMATION_DELAY';PANEL='REOBSERVE_COMPLETED_CAUSAL_PANEL'
RULES={ABS:dict(sell_share_min=.6,volume_ratio_min=1.5,absolute_hour_impact_max=.002),LAG:dict(btc_return_min=.02,btc_volume_ratio_min=1.5,lag_residual_max=-.01),PANEL:{}}

def recompute(root,spec,trial):
    assert spec['spec_hash']==digest({k:v for k,v in spec.items() if k not in ('id','spec_hash','stored_at')})
    assert trial['spec_hash']==spec['spec_hash'] and trial['spec_id']==spec['id']
    assert spec['selection_rule']==dict(id=spec['mechanism_id'],parameters=RULES[spec['mechanism_id']])
    assert digest(spec['source_receipts'])==spec['input_binding']['source_hash']
    assert instant(spec['input_binding']['available_at'])<=instant(spec['registered_at'])<=instant(trial['completed_at'])
    assert spec['ai']['status']=='NOT_REQUESTED' and spec['resource_limits']['model_calls']==0
    if spec.get('sampling_protocol'):
        import importlib.util
        module_spec=importlib.util.spec_from_file_location('event_audit',Path(__file__).with_name('verify_event_scan.py'))
        module=importlib.util.module_from_spec(module_spec);module_spec.loader.exec_module(module)
        if spec.get('action')!='EVALUATE_EVENTS':return audit_diagnostic(spec,trial)
        return module.verify_event(root,spec,trial)
    if spec['mechanism_id']==PANEL:
        assert trial['status']=='DATA_OBSERVED' and trial['counts']['historical_hypothesis_trials']==0
        for receipts in trial['source_receipts'].values():
            for r in receipts:
                assert instant(spec['registered_at'])<=instant(r['received_at'])<=instant(trial['completed_at'])
                assert sha((root/'raw/batch'/(r['sha256']+'.bin')).read_bytes())==r['sha256']
        return None
    assert spec['source_receipts']==trial['source_receipts']
    assert spec['origin']=='HISTORICAL_EXPLORATION_REUSED_NOT_INDEPENDENT'
    assert spec['entry_delay_seconds']==7200 and spec['horizon_seconds']==86400
    assert spec['costs']==dict(capital_usdt=10000,exposure_fraction=.125,fee_bps_per_side=10,slippage_bps_per_side=5,assumed_half_spread_bps=1,information_cost_per_call_usdt=.1,information_cost_status='SENSITIVITY_NOT_INVOICE',spot_funding_cashflow=0,accounting='FIXED_NOTIONAL_NOT_COMPOUNDED')
    data={};decoded={}
    for symbol,receipts in spec['source_receipts'].items():
        values=[]
        for r in receipts:
            raw=(root/'raw/batch'/(r['sha256']+'.bin')).read_bytes();assert sha(raw)==r['sha256']
            assert instant(r['received_at'])<=instant(spec['registered_at']);values.append(json.loads(raw))
        bars,funding=values;decoded[symbol]=dict(bars=bars,funding=funding)
        assert len({b[0] for b in bars})==len(bars)
        assert all(b[6]==b[0]+3599999 and b[6]<instant(receipts[0]['received_at']).timestamp()*1000 for b in bars)
        data[symbol]=dict(bars={b[0]:b for b in bars},funding=funding)
    assert set(data)==set(spec['universe'])=={'BTCUSDT','ETHUSDT','SOLUSDT'}
    assert digest(decoded)==trial['input_hash']
    common=set.intersection(*(set(d['bars']) for d in data.values()));end=max(common);expected=[]
    for i in range(14):
        signal=end-(27-2*i)*86400000;entry=signal+7200000;exit=entry+86400000
        if all(all(t in d['bars'] for t in (signal-86400000,signal-3600000,entry,exit)) and len([f for f in d['funding'] if f['fundingTime']<=signal])>=3 and signal-max(f['fundingTime'] for f in d['funding'] if f['fundingTime']<=signal)<=43200000 for d in data.values()):expected.append(signal)
    assert [r['signal_ms'] for r in trial['contexts']]==expected
    arms=['PRICE_ONLY','ECONOMIC_RULE','AI_UNAVAILABLE','CASH'];assert spec['comparison_arms']==arms
    rows={(r['case_id'],r['arm']):r for r in trial['rows']}
    assert len(rows)==len(trial['rows'])==len(expected)*4
    assert set(rows)=={(str(t),a) for t in expected for a in arms}
    totals=dict.fromkeys(arms,0.);trades=dict.fromkeys(arms,0)
    for context in trial['contexts']:
        signal=context['signal_ms'];assert context['id']==str(signal) and context['entry_ms']==signal+7200000 and context['exit_ms']==signal+93600000
        values={}
        for symbol,d in data.items():
            b=d['bars'];last=b[signal-3600000];quote=float(last[7]);assert quote>0 and 0<=float(last[10])<=quote
            v=dict(past_24h_return=float(last[4])/float(b[signal-86400000][1])-1,aggressive_sell_share=1-float(last[10])/quote,
                volume_ratio=quote/(sum(float(b[t][7]) for t in range(signal-25*3600000,signal-3600000,3600000))/24),hour_return=float(last[4])/float(last[1])-1,
                quote_volume=quote,taker_buy_quote=float(last[10]),last_feature_close_ms=last[6])
            fs=sorted([f for f in d['funding'] if f['fundingTime']<=signal],key=lambda f:f['fundingTime'])[-3:]
            v.update(latest_funding=float(fs[-1]['fundingRate']),last_three_funding_sum=sum(float(f['fundingRate']) for f in fs),funding_source_time=fs[-1]['fundingTime'])
            assert set(v)==set(context['assets'][symbol])
            for k,x in v.items():close(context['assets'][symbol][k],x)
            values[symbol]=v
        ranked=sorted(values,key=lambda s:(values[s]['past_24h_return'],s))
        price=ranked[0] if values[ranked[0]]['past_24h_return']<0 else None
        if spec['mechanism_id']==ABS:
            eligible=[s for s in ranked if values[s]['past_24h_return']<0 and values[s]['aggressive_sell_share']>=.6 and values[s]['volume_ratio']>=1.5 and abs(values[s]['hour_return'])<=.002]
        else:
            btc=values['BTCUSDT'];eligible=[]
            if btc['past_24h_return']>=.02 and btc['volume_ratio']>=1.5:
                eligible=sorted([s for s in ('ETHUSDT','SOLUSDT') if values[s]['past_24h_return']-btc['past_24h_return']<=-.01],key=lambda s:(values[s]['past_24h_return']-btc['past_24h_return'],s))
        choices=dict(PRICE_ONLY=price,ECONOMIC_RULE=eligible[0] if eligible else None,CASH=None)
        missing=rows[(context['id'],'AI_UNAVAILABLE')];assert missing['status']=='NOT_RUN' and missing['net_usdt'] is None and missing['information_cost_usdt']==0
        for arm,symbol in choices.items():
            r=rows[(context['id'],arm)];assert r['symbol']==symbol and r['action']==('LONG' if symbol else 'NO_TRADE') and r['not_a_fill']
            qty=gross=cost=0.
            if symbol:
                entry=float(data[symbol]['bars'][context['entry_ms']][1]);exit=float(data[symbol]['bars'][context['exit_ms']][1]);qty=1250/(entry*1.0016);gross=qty*(exit-entry);cost=qty*(entry+exit)*.0016
                close(r['entry_price'],entry);close(r['exit_price'],exit);trades[arm]+=1
            else:assert r['entry_price'] is None and r['exit_price'] is None
            for k,x in dict(quantity=qty,gross_usdt=gross,trading_cost_usdt=cost,information_cost_usdt=0,net_usdt=gross-cost).items():close(r[k],x)
            assert r['entry_ms']==context['entry_ms'] and r['exit_ms']==context['exit_ms'];totals[arm]+=gross-cost
    totals['AI_UNAVAILABLE']=None
    for arm,net in totals.items():
        a=trial['aggregates'][arm];assert a['trades']==trades[arm] and a['units']==(0 if arm=='AI_UNAVAILABLE' else len(expected))
        if net is None:assert a['net_usdt'] is None
        else:close(a['net_usdt'],net)
    assert trial['status']==('REQUIRES_NEW_FORWARD_VALIDATION' if totals['ECONOMIC_RULE']>max(0,totals['PRICE_ONLY']) else 'REJECTED_EXPLORATORY')
    assert not trial['adopted'] and not trial['independent_validation']
    assert trial['counts']==dict(historical_hypothesis_trials=1,historical_windows=len(expected),historical_hypothetical_entries=sum(trades.values()),independent_forward_events=0,paper_fills=0,matured_forward_results=0,data_collection_runs=0,model_calls=0)
    return totals

COUNT_KEYS=('historical_hypothesis_trials','historical_windows','historical_hypothetical_entries','independent_forward_events','paper_fills','matured_forward_results','data_collection_runs','model_calls')
ERRORS={'DATA_INSUFFICIENT','EXECUTION_ERROR','EXPIRED'}

def audit_diagnostic(spec,trial):
    assert trial['action']==spec['action'] and trial['status'] in ('DIAGNOSTIC_COMPLETED','FORWARD_CANDIDATE_FROZEN')
    assert trial['counts']==dict.fromkeys(COUNT_KEYS,0) and not trial['adopted'] and not trial['independent_validation']
    assert trial['ai_status']=='BLOCKED_MODEL_CALL_CAP_ZERO'
    return None


def audit_coverage(specs,trials,receipts,requests,request_results,recalculate):
    # A status is not permission to omit a trial. Compare immutable receipts,
    # request membership, statuses and allowed error payloads before arithmetic.
    def indexed(rows):
        d={r['id']:r for r in rows};assert len(d)==len(rows),'DUPLICATE_AUDIT_ID';return d
    ts=indexed(trials);rs=indexed(receipts);qs=indexed(requests);ends=indexed(request_results)
    expected=set(ts)|set(rs)|{k for q in request_results for k in q['trial_ids']};coverage=[];values={}
    for key in sorted(expected):
        category='missing';detail=None
        try:
            assert key in ts,'MISSING_TRIAL'
            t=ts[key];spec=specs[key];assert t['parent_trial_id']==spec['parent_trial_id'] and t['source_version']==spec['source_version']
            assert t['request_id'] in qs and t['request_id'] in ends,'MISSING_REQUEST_OR_COMPLETION'
            end=ends[t['request_id']];assert end['trial_ids'].count(key)==1,'REQUEST_MEMBERSHIP'
            assert digest({k:v for k,v in spec.items() if k not in ('id','spec_hash','stored_at')})==spec['spec_hash']
            if key in rs:
                r=rs[key];assert r['request_id']==t['request_id'] and r['input_hash']==spec['spec_hash'] and r['output_hash']==digest(r['result'])
                payload={k:v for k,v in t.items() if k not in ('id','request_id','parent_trial_id','source_version','completed_at','stored_at')}
                assert payload==r['result'],'RECEIPT_TRIAL_PAYLOAD_MISMATCH'
            else:
                assert t['status']=='EXPIRED' and t['resource_accounting']=='NO_EXECUTION_NO_NEW_RESERVATION','MISSING_EXECUTION_RECEIPT'
                assert instant(t['completed_at'])>instant(spec['expiry'])
            if t['status'] in ERRORS:
                allowed={'id','request_id','parent_trial_id','source_version','completed_at','stored_at','status','error','missing','spec_id','spec_hash','mechanism_id','source_receipts','counts','resource_accounting','action'}
                assert set(t)<=allowed,'FINANCIAL_PAYLOAD_HIDDEN_AS_ERROR'
                assert not t.get('counts') and t['source_receipts']==spec['source_receipts']
                if t['status']!='EXPIRED':assert isinstance(t.get('error'),str) and t['error'],'ERROR_REASON_REQUIRED'
                category='data_insufficient' if t['status']=='DATA_INSUFFICIENT' else 'valid_error_or_expired'
                detail=t.get('error','Expired without a financial result')
            else:
                values[key]=recalculate(spec,t);category='numerical_audited' if values[key] is not None else 'nonfinancial_audited'
        except (AssertionError,KeyError,ValueError,TypeError) as exc:
            category='missing' if key not in ts else 'rejected_inconsistent';detail=str(exc)
        coverage.append(dict(trial_id=key,category=category,detail=detail))
    request_errors=[]
    for qid,result in ends.items():
        try:
            assert qid in qs
            used=[r for r in trials if r['request_id']==qid]
            assert result['trial_ids']==[r['id'] for r in used]
            assert result['statuses']=={st:sum(r['status']==st for r in used) for st in sorted({r['status'] for r in used})},'REQUEST_STATUS_COUNTS'
            for k in COUNT_KEYS:assert result['counts'][k]==sum(r.get('counts',{}).get(k,0) for r in used)
        except (AssertionError,KeyError,TypeError) as exc:request_errors.append(dict(request_id=qid,error=str(exc)))
    report=dict(expected_trial_ids=sorted(expected),trials=coverage,counts={cat:sum(r['category']==cat for r in coverage) for cat in ('numerical_audited','valid_error_or_expired','data_insufficient','nonfinancial_audited','rejected_inconsistent','missing')},request_errors=request_errors,
        noncomputable_trial_ids=[r['trial_id'] for r in coverage if r['category'] in ('valid_error_or_expired','data_insufficient')])
    assert len(coverage)==len(expected)
    assert not request_errors and not any(r['category'] in ('missing','rejected_inconsistent') for r in coverage),report
    return report,values


def verify(root):
    root=Path(root).resolve();manifest=json.loads((root/'manifest.json').read_bytes())
    for path,h in manifest.items():
        p=(root/path).resolve();assert p.is_relative_to(root) and sha(p.read_bytes())==h,path
    ledgers={}
    for p in (root/'ledgers').glob('*.jsonl'):
        rows=[json.loads(x) for x in p.read_text('utf-8').splitlines()];assert len({(r['kind'],r['id']) for r in rows})==len(rows)
        for r in rows:
            assert digest(r['body'])==r['export_body_sha256']
            if not r['redacted']:assert r['export_body_sha256']==r['original_body_sha256']
        ledgers[p.stem]=rows
    records=lambda name,kind:[r['body'] for r in ledgers[name] if r['kind']==kind]
    source=root/'source-current/one_trading';h=hashlib.sha256()
    for p in sorted((source/'alpha_loop').rglob('*.py'))+sorted((source/'autonomy').glob('*.py'))+sorted((source/'economic_research').glob('*.py')):h.update(p.relative_to(source).as_posix().encode());h.update(p.read_bytes())
    base=h.hexdigest();assert base==records('alpha-valid','alpha_protocol')[0]['implementation_hash']
    versions={}
    for c in records('batch','batch_config'):
        names=c.get('source_files') or ('batch_analysis.py','economic_batches.py')+(('batch_control.py','batch_models.py','hypothesis_loop.py') if c.get('execution_contract_version')==2 else ())
        h=hashlib.sha256(base.encode())
        for name in names:h.update((root/'source-versions'/c['source_hash']/name).read_bytes())
        assert h.hexdigest()==c['source_hash'];versions[c['source_hash']]=c
    for name,rows in ledgers.items():
        for r in rows:
            if r['kind'] in ('http_receipt','retrieval_receipt','flow_frame') and r['body'].get('sha256'):
                assert sha((root/'raw'/name/(r['body']['sha256']+'.bin')).read_bytes())==r['body']['sha256']
    specs={s['id']:s for s in records('batch','hypothesis_spec')};parents={r['id']:r for r in records('batch','trial_result')+records('batch','hypothesis_trial')};results={}
    for spec in specs.values():
        assert spec['spec_hash']==digest({k:v for k,v in spec.items() if k not in ('id','spec_hash','stored_at')})
        assert spec['source_version'] in versions and spec['parent_result_hash']==digest(parents[spec['parent_trial_id']])
        assert instant(spec['registered_at'])>=instant(parents[spec['parent_trial_id']]['stored_at'])
    requests=records('batch','research_request_result')
    coverage,results=audit_coverage(specs,records('batch','hypothesis_trial'),records('batch','hypothesis_receipt'),
        records('batch','research_request'),requests,lambda spec,trial:recompute(root,spec,trial))
    # Raw model answer, recorded answer and all old policy selections are joined.
    models={r['id']:r for r in records('batch','model_call')};inputs={}
    for key,call in models.items():
        folder=root/'models/batch'/key;payload=json.loads((folder/'input.json').read_bytes());inputs[key]=payload
        assert digest(payload)==call['input_hash'] and sha((folder/'prompt.txt').read_bytes())==call['prompt_hash']
        if call['status']=='CONNECTED_VALID':
            answer=json.loads((folder/'answer.json').read_bytes());assert answer==call['answer'] and digest(answer)==call['response_hash']
    for trial in records('batch','trial_result'):
        if not trial.get('model_call_id'):continue
        call=models[trial['model_call_id']]
        if trial['family']=='funding_crowding':
            assert call['answer']['rule']==trial['selected_ai_rule'] and inputs[call['id']]['earliest_context']==trial['contexts'][0]
            data={s:dict(zip(('bars','funding'),[json.loads((root/'raw/batch'/(r['sha256']+'.bin')).read_bytes()) for r in rs])) for s,rs in trial['source_receipts'].items()}
            assert digest(data)==trial['input_hash'] and inputs[call['id']]['source_hashes']=={s:[r['sha256'] for r in rs] for s,rs in trial['source_receipts'].items()}
        elif trial['family']=='credit_growth':
            assert call['input_hash']==trial['input_hash']
            answer=call['answer']['decisions'];assert len(answer)==1 and answer[0]['id']==inputs[call['id']]['cases'][0]['id'] and answer[0]['symbol']=='AAVEUSDT'
            for r in trial['comparison']:
                if r['arm'] in ('G_AI','G_LAGGED'):assert r['decision']['action']==answer[0]['action']
    growth=records('batch','growth_bridge_result');exits={r['id']:r for r in records('batch','growth_bridge_exit')};costs={}
    links={r['id']:t['model_call_id'] for t in records('batch','trial_result') if t.get('family')=='credit_growth' for r in t['comparison'] if r['arm'] in ('G_AI','G_LAGGED')}
    for arm in sorted({r['arm'] for r in growth}):
        charges={};pnl=0.;opened=[]
        for r in (r for r in growth if r['arm']==arm):
            call=r['decision'].get('model_call_id') or links.get(r['id']) or r['id'];cost=r['information_cost_usdt']
            if arm in ('G_AI','G_LAGGED'):assert call in models;close(cost,.1)
            else:close(cost,0)
            if call in charges:close(charges[call],cost)
            charges[call]=cost
            if r['status']=='FILLED':
                assert r['decision']['action']=='LONG';f=r['fill'];close(f['cash_delta_usdt'],-f['gross_usdt']-f['fee_usdt'])
                assert instant(f['quote_received_at'])>=instant(r['eligible_at'])
                if r['decision'].get('expires_at'):assert instant(f['quote_received_at'])<=instant(r['decision']['expires_at'])
                if r['id'] in exits:pnl+=f['cash_delta_usdt']+exits[r['id']]['fill']['cash_delta_usdt']
                else:opened.append(r['id'])
        costs[arm]=dict(information_cost_usdt=sum(charges.values()),policy_net_usdt=None if opened else pnl-sum(charges.values()))
    expected=json.loads((root/'policy-costs.json').read_bytes())
    for arm,result in costs.items():
        close(expected['arms'][arm]['information_cost_usdt'],result['information_cost_usdt'])
        if result['policy_net_usdt'] is not None:close(expected['arms'][arm]['policy_net_usdt'],result['policy_net_usdt'])
    assert expected['actual_model_attempts']==len(records('batch','batch_model_intent')) and not expected['unallocated_call_ids']
    all_costs=json.loads((root/'all-policy-costs.json').read_bytes());assert all_costs['batch']==expected
    for name in ('alpha-valid','alpha-invalid'):
        config=records(name,'alpha_protocol')[0]['config'];calls={r['id']:r for r in records(name,'model_call')};report=all_costs['alpha'][name]
        assert report['actual_model_calls']==len(calls) and report['actual_billed_usdt'] is None
        close(report['assumed_cost_usdt'],len(calls)*config['information_cost_usdt_per_model_call'])
        for key,call in calls.items():
            folder=root/'models'/name/key;assert digest(json.loads((folder/'input.json').read_bytes()))==call['input_hash']
            if call['status']=='CONNECTED_VALID':
                answer=json.loads((folder/'answer.json').read_bytes());assert answer==call['answer'] and digest(answer)==call['response_hash']
        for arm in config['arms']:
            ds=[d for d in records(name,'alpha_decision') if d['arm']==arm];ids=sorted({key for d in ds for key in d.get('model_call_ids',[])})
            assert all(key in calls for key in ids);a=report['policies'][arm]
            assert a['actual_model_call_ids']==ids and a['decision_count']==len(ds) and a['actions']==[d['action'] for d in ds]
            close(a['information_cost_usdt'],len(ids)*config['information_cost_usdt_per_model_call'])
    preservation=json.loads((root/'preservation.json').read_bytes());authority=json.loads((root/'authority.json').read_bytes())
    for name,prefix in preservation['original_record_prefixes'].items():
        after={str(r['sequence']):r['original_body_sha256'] for r in ledgers[name]};assert all(after[k]==v for k,v in prefix.items())
    assert authority['alpha_registry'][:len(preservation['alpha_registry_before'])]==preservation['alpha_registry_before']
    for budget in authority['engine_budgets']:
        tasks=[t for t in authority['tasks'] if next(e for e in authority['experiments'] if e['id']==t['experiment_id'])['engine']==budget['engine']]
        assert budget['time_spent']==sum(t['actual_time'] or 0 for t in tasks if t['state'] in ('DONE','FAILED'))
        assert budget['time_reserved']==sum(json.loads(t['task_json'])['time_seconds'] for t in tasks if t['state']=='CLAIMED')
    receipt_ids={r['id'] for r in records('batch','hypothesis_receipt')}
    assert all(t['task_key'][6:] in receipt_ids for t in authority['tasks'] if t['task_key'].startswith('child:') and t['state'] in ('DONE','FAILED')),'COMPLETED_AUTHORITY_TASK_MISSING_RECEIPT'
    for receipt in records('batch','hypothesis_receipt'):
        task=next(t for t in authority['tasks'] if t['task_key']=='child:'+receipt['id']);assert task['actual_time']==receipt['elapsed_seconds'] and json.loads(task['result_json'])==receipt['result']
    return dict(status='PASS',audit_coverage=coverage,typed_trial_results=results,policy_costs=costs,requests=len(requests),original_record_prefixes_preserved=True,forward_edge_proven=False)

if __name__=='__main__':print(json.dumps(verify(sys.argv[1]),ensure_ascii=False,indent=2))
