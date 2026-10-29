"""Economic batch calculations. Historical reconstruction is not forward evidence."""
import math

def design_diagnostic(config,alpha,lambdas=(.1,.25,.5,.75)):
    n=int(config['research_days']*86400/config['horizon_seconds'])
    threshold=1/alpha;scale=config['return_scale']
    def log_e(effect,count,increment=False):
        x=min(1,max(-1,effect/scale))/(2 if increment else 1)
        logs=[count*math.log1p(l*x) for l in lambdas];m=max(logs)
        return m+math.log(sum(math.exp(y-m) for y in logs)/len(logs))
    def needed(effect,increment):
        lo,hi=0,1
        while hi<10000000 and log_e(effect,hi,increment)<math.log(threshold):hi*=2
        while hi-lo>1:
            mid=(lo+hi)//2
            if log_e(effect,mid,increment)>=math.log(threshold):hi=mid
            else:lo=mid
        return hi
    def effect_needed(increment):
        lo,hi=0.,scale*(2 if increment else 1)
        for _ in range(80):
            mid=(lo+hi)/2
            if log_e(mid,n,increment)>=math.log(threshold):hi=mid
            else:lo=mid
        return hi
    return dict(scope='DESIGN_DIAGNOSTIC_NOT_INVESTMENT_RESULTS',planned_maximum_nonoverlapping_units=n,
        minimum_units=config['minimum_clusters'],alpha=alpha,threshold=threshold,return_scale=scale,
        deterministic_effect_required_per_unit=dict(absolute=effect_needed(False),incremental=effect_needed(True)),
        effects=[dict(capital_net_effect=e,absolute_units=needed(e,False),incremental_units=needed(e,True),
                      absolute_e_at_planned_n=math.exp(log_e(e,n)),incremental_e_at_planned_n=math.exp(log_e(e,n,True)))
                 for e in (.0001,.0005,.001,.0025,.005)],
        assumptions='Constant noiseless positive capital returns, not statistical power; real variance/abstentions reduce feasibility. Increment stream divides clipped utility difference by two.',
        retrospective_threshold_change=False)

def historical_contexts(data):
    """14 disjoint 48h blocks; 24h outcomes enter 2h after signal, no nearby bar fill."""
    universe=sorted(data);bars={s:{r[0]:r for r in data[s]['bars']} for s in universe}
    common=set.intersection(*(set(b) for b in bars.values()));end=max(common)
    signals=[end-(27-2*i)*3600000*24 for i in range(14)]
    result=[]
    for signal in signals:
        assets={};valid=True
        for s in universe:
            b=bars[s];past=signal-24*3600000;entry=signal+2*3600000;exit=entry+24*3600000
            if any(t not in b for t in (past,signal-3600000,entry,exit)):valid=False;break
            funding=sorted([r for r in data[s]['funding'] if r['fundingTime']<=signal],key=lambda r:r['fundingTime'])
            if len(funding)<3:valid=False;break
            if signal-funding[-1]['fundingTime']>12*3600000:valid=False;break
            ret=float(b[signal-3600000][4])/float(b[past][1])-1
            assets[s]=dict(past_24h_return=ret,latest_funding=float(funding[-1]['fundingRate']),
                           last_three_funding_sum=sum(float(r['fundingRate']) for r in funding[-3:]),
                           funding_source_time=funding[-1]['fundingTime'])
        if valid:result.append(dict(id=str(signal),signal_ms=signal,entry_ms=entry,exit_ms=exit,assets=assets))
    return result

def historical_comparison(contexts,data,ai,information_cost=.1,capital=10000,exposure=.125):
    byid={r['id']:r for r in ai} if ai is not None else {}
    prices={s:{r[0]:r for r in d['bars']} for s,d in data.items()}
    arms=('PRICE_ONLY','FUNDING_RULE','AI_RETROSPECTIVE','CASH');rows=[]
    charge=information_cost/len(contexts) if contexts else 0
    for c in contexts:
        ordered=sorted(c['assets'],key=lambda s:(c['assets'][s]['past_24h_return'],s))
        baseline=ordered[0] if c['assets'][ordered[0]]['past_24h_return']<0 else None
        eligible=[s for s in ordered if c['assets'][s]['past_24h_return']<0 and c['assets'][s]['last_three_funding_sum']<0]
        a=byid.get(c['id']);selected={'PRICE_ONLY':baseline,'FUNDING_RULE':eligible[0] if eligible else None,
             'AI_RETROSPECTIVE':a['symbol'] if a and a['action']=='LONG' else None,'CASH':None}
        for arm in arms:
            if arm=='AI_RETROSPECTIVE' and a is None:
                rows.append(dict(case_id=c['id'],arm=arm,status='MODEL_UNAVAILABLE',net_usdt=None));continue
            symbol=selected[arm];info=charge if arm=='AI_RETROSPECTIVE' else 0.
            gross=cost=0.;entry=exit=None
            if symbol:
                entry=float(prices[symbol][c['entry_ms']][1]);exit=float(prices[symbol][c['exit_ms']][1])
                # Each side: fee 10bps, slippage 5bps, assumed half-spread 1bp.
                initial=capital*exposure;qty=initial/(entry*1.0016)
                gross=qty*(exit-entry);cost=qty*(entry+exit)*.0016
            rows.append(dict(case_id=c['id'],arm=arm,status='HISTORICAL_RECONSTRUCTION',symbol=symbol,
                entry_ms=c['entry_ms'],exit_ms=c['exit_ms'],entry_price=entry,exit_price=exit,
                gross_usdt=gross,trading_cost_usdt=cost,information_cost_usdt=info,net_usdt=gross-cost-info,
                not_a_fill=True,spread_status='ASSUMED_NOT_HISTORICAL_BOOK',funding_cashflow=0,
                funding_status='SPOT_HOLDER_DOES_NOT_RECEIVE_PERP_FUNDING'))
    aggregates={a:dict(units=sum(r['arm']==a and r['net_usdt'] is not None for r in rows),
        trades=sum(r['arm']==a and bool(r.get('symbol')) for r in rows),
        net_usdt=sum(r['net_usdt'] for r in rows if r['arm']==a and r['net_usdt'] is not None)) for a in arms}
    for a in aggregates:
        if aggregates[a]['units']==0:aggregates[a]['net_usdt']=None
    return dict(units=len(contexts),rows=rows,aggregates=aggregates,independent_forward_units=0,
        regime_and_cross_asset_dependence_unresolved=True,historical_llm_contamination_possible=True,
        execution='HOURLY_OPEN_RECONSTRUCTION_TWO_HOUR_DELAY_NOT_EXECUTABLE_QUOTES',
        model_native_latency_not_reconstructable=True)

def credit_gate(observation,history,registered_at):
    """Ethereum USDC credit stocks are narrower than attributable holder cashflow."""
    if observation['status']!='MEASURED':return dict(state='UNKNOWN',missing=observation.get('missing',['chain state']),allow_entry=False)
    current=observation['points'][-1];older=observation['points'][0]
    change={k:current['values'][k]/older['values'][k]-1 for k in ('usdc_supply','aave_usdc_debt','aave_usdc_deposits')}
    from datetime import datetime
    parse=lambda t:datetime.fromisoformat(t.replace('Z','+00:00')).timestamp()
    eligible=[o for o in history if o.get('status')=='MEASURED' and parse(o['observed_at'])>=parse(registered_at)
              and o['points'][0]['timestamp']>=parse(registered_at)]
    eligible.sort(key=lambda o:o['points'][-1]['timestamp'])
    # Distinct observations need >=24h actual source-event separation; historical
    # archive points fetched together cannot satisfy a prospective confirmation.
    independent=(len(eligible)>=2 and eligible[-1]['points'][0]['timestamp']>=eligible[-2]['points'][-1]['timestamp']
                 and eligible[-1]['points'][-1]['timestamp']-eligible[-2]['points'][-1]['timestamp']>=86400
                 and all(eligible[-2]['points'][-1]['values'][k]>eligible[-2]['points'][0]['values'][k] for k in ('usdc_supply','aave_usdc_debt')))
    missing=['Attributable new borrow principal separate from accrued interest',
             'Incremental revenue retained for AAVE holders and executed token demand',
             'Circle-program attribution; Ethereum is not global USDC supply']
    if not independent:missing.append('Two post-registration daily observations')
    joint=change['usdc_supply']>0 and change['aave_usdc_debt']>0
    return dict(state='TRUE' if joint else 'FALSE',allow_entry=independent and joint,stock_changes=change,
        observed_stock_direction='INCREASE' if change['usdc_supply']>0 and change['aave_usdc_debt']>0 else 'NOT_JOINT_INCREASE',
        missing=missing,explanation='This narrow stock-direction predicate is measured; it does not establish causal attribution or holder value. Separate AI/value and delayed-entry gates remain mandatory.')

