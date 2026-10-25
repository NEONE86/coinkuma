"""Independent process: source bytes, ABI/CID, chronology, controls and labels.

Same-author audit, not an external audit. Does not call production proof(),
schedule(), select_controls(), or labels(). Read-only source DB and atlas.
"""
from __future__ import annotations
import argparse
from collections import Counter
from datetime import datetime,timezone
import gzip
import hashlib
import json
import math
import statistics
from pathlib import Path
import subprocess
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from one_trading.historical_data import load_histories
from scripts.validate_repricing_v3 import IndependentSource,eq_tree
from one_trading.value_evidence import save_json

DAY=86400000
PARENT='35bfbbc2a71aa47579974d15e9d1c62e3ba7db55'


def ts(value):return int(datetime.fromisoformat(value.replace('Z','+00:00')).timestamp()*1000)
def digest(b):return hashlib.sha256(b).hexdigest()
def read(p):return json.loads(Path(p).read_text(encoding='utf-8'))
def check(condition,message):
    if not condition:raise AssertionError(message)


def raw(root,hash):
    b=(root/'raw'/(hash+'.bin')).read_bytes()
    check(digest(b)==hash,'raw_bytes_changed')
    return b


def verify_proof(root,proof):
    sources=proof['sources']; decoded=[(x,json.loads(raw(root,x['sha256']))) for x in sources]
    if 'transaction_hash' not in proof:
        b=next(j for x,j in decoded if 'blockscout' in x['url'])
        other=next(j for x,j in decoded if 'routescan' in x['url'])
        check(b['hash']==proof['block_hash'],'activation_hash')
        check(ts(b['timestamp'])==int(other['result']['timeStamp'])*1000==ts(proof['published_at']),'activation_time')
        prev=next(j for x,j in decoded if x['url'].endswith('/12964999'))
        check(b['parent_hash']==prev['hash'] and prev['base_fee_per_gas'] is None and b['base_fee_per_gas']=='1000000000','london_activation_transition')
        return None
    envelope=next(j for x,j in decoded if '/logs?' in x['url'])
    log=next(r for r in envelope['items'] if r['transaction_hash']==proof['transaction_hash'] and r['index']==proof['log_index'])
    block=next(j for x,j in decoded if 'blockscout.com/api/v2/blocks/' in x['url'])
    tx=next(j for x,j in decoded if 'blockscout.com/api/v2/transactions/' in x['url'])
    receipt=next(j['result'] for x,j in decoded if 'action=eth_getTransactionReceipt' in x['url'])
    otherblock=next(j['result'] for x,j in decoded if 'action=getblockreward' in x['url'])
    check(block['hash']==log['block_hash']==receipt['blockHash']==proof['block_hash'],'cross_source_block_hash')
    check(block['height']==tx['block_number']==int(receipt['blockNumber'],16)==proof['block_number'],'cross_source_block_number')
    check(receipt['transactionHash'].lower()==tx['hash'].lower()==proof['transaction_hash'].lower(),'cross_source_tx_hash')
    check(tx['status']=='ok' and receipt['status']=='0x1','successful_receipt')
    check(ts(log['block_timestamp'])==ts(block['timestamp'])==ts(tx['timestamp'])==int(otherblock['timeStamp'])*1000==ts(proof['published_at']),'cross_source_time')
    matched=[r for r in receipt['logs'] if int(r['logIndex'],16)==proof['log_index']]
    check(len(matched)==1,'unique_receipt_log');r=matched[0]
    check(not r.get('removed',False) and r['address'].lower()==log['address']['hash'].lower(),'log_address_or_removed')
    check(r['data'].lower()==log['data'].lower() and r['topics']==[t for t in log['topics'] if t],'exact_log_bytes')
    return log


def cid_from_bytes(b,cid):
    # Independently decode the multihash and construct length-delimited protobuf.
    alphabet='123456789ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz'
    n=0
    for ch in cid:n=n*58+alphabet.index(ch)
    mh=n.to_bytes((n.bit_length()+7)//8,'big')
    def vint(x):
        values=[]
        while x>=128:values.append((x%128)+128);x//=128
        return bytes(values+[x])
    unix=bytes([8,2,18])+vint(len(b))+b+bytes([24])+vint(len(b))
    node=bytes([10])+vint(len(unix))+unix
    check(mh[:2]==b'\x12\x20' and mh[2:]==hashlib.sha256(node).digest(),'independent_ipfs_preimage')
    return mh[2:]


def audit(evidence,output,atlas,archive):
    receipts=[json.loads(line) for line in (evidence/'requests.jsonl').read_text(encoding='utf-8').splitlines()]
    valid=[r for r in receipts if r.get('status')==200 and 'path' in r and not r.get('error')]
    first={};unique=set()
    for r in valid:
        raw(evidence,r['sha256']);unique.add(r['sha256'])
        check(ts(r['requested_at'])<=ts(r['received_at']),'receipt_chronology')
        key=(r['url'],r['sha256']);first[key]=min(first.get(key,r['received_at']),r['received_at'])
    events=read(evidence/'event_panel.json');proofs=0
    def each_proof(obj):
        if isinstance(obj,dict):
            if 'sources' in obj and 'block_hash' in obj:yield obj
            else:
                for v in obj.values():yield from each_proof(v)
        elif isinstance(obj,list):
            for v in obj:yield from each_proof(v)
    for e in events:
        for p in each_proof(e):
            verify_proof(evidence,p);proofs+=1
            for s in p['sources']:
                check(s['first_seen_at']==first[(s['url'],s['sha256'])],'historical_first_seen_backfill')
        if not e['source_time_proven']:
            check(e['published_at'] is None,'unproven_publication_not_null')
            continue
        if e['symbol']=='UNIUSDT':
            p=e['publication_proof'];log=verify_proof(evidence,p)
            # Independent hexadecimal ABI decoding, not production decoder.
            hexdata=log['data'][2:];offset=int(hexdata[512:576],16)*2
            length=int(hexdata[offset:offset+64],16)
            description=bytes.fromhex(hexdata[offset+64:offset+64+2*length])
            check(digest(description)==e['source_text_sha256'],'original_description_hash')
            check(int(hexdata[:64],16)==e['proposal_id'],'uni_proposal_id')
            check(e['first_seen_at']==first[(e['source_url'],e['source_envelope_sha256'])],'uni_first_seen')
        elif e['symbol']=='AAVEUSDT':
            log=verify_proof(evidence,e['publication_proof'])
            b=raw(evidence,e['source_text_sha256'])
            check(cid_from_bytes(b,e['ipfs_cid'])==bytes.fromhex(log['data'][2:]),'onchain_cid_commitment')
            check(int(log['topics'][1],16)==e['proposal_id'],'aave_proposal_id')
            check(e['first_seen_at']==first[(e['source_url'],e['source_text_sha256'])],'aave_first_seen')
            for payload in e['payloads']:
                send=verify_proof(evidence,payload['sent_proof'])
                execute=verify_proof(evidence,payload['execution_proof'])
                check(int(send['topics'][1],16)==e['proposal_id'],'payload_governance_link')
                check(int(send['data'][2:66],16)==payload['id']==int(execute['data'],16),'payload_execution_id')
                check(e['executed_at']==execute['block_timestamp'],'payload_actual_time')
                check(ts(e['executed_at'])>=ts(e['governance_executed_at']),'payload_execution_after_dispatch')
        if e.get('executed_at'):
            check(ts(e['executed_at'])>=ts(e['published_at']),'execution_before_publication')
        check(e['historical_collector_first_seen_at'] is None,'unknown_historical_observation_fabricated')
        if e.get('economic_execution_evidence'):
            ec=e['economic_execution_evidence'];record=next(r for r in valid if r['url']==ec['receipt_url'])
            receipt=json.loads(raw(evidence,record['sha256']))['result']
            log=next(x for x in receipt['logs'] if int(x['logIndex'],16)==ec['log_index'])
            check(str(int(log['data'],16))==ec['amount_base_units'],'economic_amount')
            if ec['kind']=='UNI_TRANSFER_TO_DEAD':check(int(log['topics'][2],16)==0xdead,'sink_recipient')
            if ec['kind']=='AETHUSDT_ALLOWANCE':check(not ec['completed_buyback_proven'],'allowance_not_purchase')
    scheduled=read(output/'scheduled_events.json');byid={r['event_id']:r for r in scheduled}
    # Independent dates, ordering, overlap and cutoff calculation.
    eras=[('development',ts('2019-01-01T00:00:00Z'),ts('2023-01-01T00:00:00Z')),
          ('validation',ts('2023-01-01T00:00:00Z'),ts('2025-01-01T00:00:00Z')),
          ('challenge',ts('2025-01-01T00:00:00Z'),ts('2026-10-01T00:00:00Z'))]
    last={};accepted=[]
    for e in sorted(events,key=lambda e:(e.get('published_at') or '9999',e['event_id'])):
        r=byid[e['event_id']]
        if not(e['source_time_proven'] and e['direct_value_accrual']):
            check(r['exclusion']=='QUALITATIVE_OR_INCIDENTAL','qualitative_gate');continue
        day=(ts(e['published_at'])+3600000)//DAY*DAY
        check(day==r['day'],'independent_signal_day')
        era=next((x for x in eras if x[1]<=day<x[2]),None)
        if era is None:check(r['exclusion']=='OUTSIDE_STUDY_PERIOD','period');continue
        key=(e['symbol'],era[0])
        if day-last.get(key,-10**18)<182*DAY:
            check(r['exclusion']=='DUPLICATE_EPISODE','episode');continue
        last[key]=day
        if day+181*DAY>=eras[-1][2]:check(r['exclusion']=='IMMATURE_180D','global_maturity')
        elif day+181*DAY>=era[2]:check(r['exclusion']=='ERA_BOUNDARY_PURGE','era_maturity')
        elif r['exclusion']!='NO_ELIGIBLE_ATLAS_ROW':
            check(r['exclusion'] is None,'unexpected_exclusion');accepted.append(e['event_id'])
        check(ts(r['earliest_hypothetical_entry'])==day+2*DAY,'D_plus_two')
    pairs=read(output/'matched_pairs_private.json');check({p['event_id'] for p in pairs}==set(accepted),'all_primary_events_linked')
    wanted={p['case']['day'] for p in pairs}
    contexts=read(output/'execution_context_private.json')
    wanted.update(r['day'] for r in contexts if r['usable'])
    cohorts={d:[] for d in wanted};atlas_rows=0
    with gzip.open(atlas,'rt',encoding='utf-8') as stream:
        for line in stream:
            row=json.loads(line);atlas_rows+=1
            if row['day'] in wanted:cohorts[row['day']].append(row)
    check(atlas_rows==473267,'atlas_row_count')
    selected={}
    for p in pairs:
        c=p['case'];day=c['day'];cohort=cohorts[day]
        check(c==next(x for x in cohort if x['symbol']==c['symbol']),'exact_atlas_join')
        excluded=set()
        for e in events:
            if e['direct_value_accrual'] and e['source_time_proven']:
                d=(ts(e['published_at'])+3600000)//DAY*DAY
                if 0<=day-d<=181*DAY:excluded.add(e['symbol'])
        candidates=[x for x in cohort if x['symbol']!=c['symbol'] and x['symbol'] not in excluded
                    and x['liquidity_bin']==c['liquidity_bin'] and x['age_bin']==c['age_bin']]
        distance=lambda x:(abs(math.log(x['liquidity']/c['liquidity']))+abs(math.log(x['history_days']/c['history_days'])),x['symbol'])
        check(p['controls']==sorted(candidates,key=distance)[:3],'outcome_blind_matching')
        failures=[x for x in candidates if not x['delayed']['180d_2x']['hit']]
        check(p['failure_only_controls']==sorted(failures,key=distance)[:3],'descriptive_failure_matching')
        for row in [c]+p['controls']+p['failure_only_controls']:selected[(row['symbol'],day)]=row
    for r in contexts:
        if r['atlas']:
            check(r['atlas']==next(x for x in cohorts[r['day']] if x['symbol']==r['symbol']),'execution_context_join')
            selected[(r['symbol'],r['day'])]=r['atlas']
    # Source prices are read-only. Reuse PR7's alternate path implementation,
    # which does not call the atlas label builder, for each linked path.
    h,_=load_histories(archive)
    source=IndependentSource(h)
    for (symbol,day),r in selected.items():
        check(symbol in source.universe[day],'historical_eligible_not_survivor')
        b=h[symbol][source.idx[symbol][day]];entry=h[symbol][source.idx[symbol][day+2*DAY]]
        eq_tree(source.path(symbol,day+DAY,b.close,.85*b.close),r['research'])
        eq_tree(source.path(symbol,day+2*DAY,entry.open,.85*b.close),r['delayed'])
        for k,v in source.features(symbol,day).items():eq_tree(v,r[k])
    stats=read(output/'statistics.json')
    for era,values in stats.items():
        subset=[p for p in pairs if era=='all' or next(name for name,a,z in eras if a<=p['case']['day']<z)==era]
        for label,reported in values.items():
            wins=sum(bool(p['case']['delayed'][label]['hit']) for p in subset)
            check(reported['events']==len(subset) and reported['hits']==wins,'statistical_counts')
            matched=[p for p in subset if p['controls']]
            differences=[float(p['case']['delayed'][label]['hit'])-sum(float(c['delayed'][label]['hit']) for c in p['controls'])/len(p['controls']) for p in matched]
            eq_tree(reported['paired_success_difference'],statistics.fmean(differences) if differences else None)
            check(reported['cluster_interval'] is None and reported['token_clusters']<10,'sparse_inference_not_overclaimed')
    strict=read(output/'collector_schedule.json')
    for r in strict:
        if r['collector_day'] is not None:
            check(r['collector_day']>=eras[-1][2] and r['exclusion']=='OUTSIDE_STUDY_PERIOD','collector_strict_gate')
    summary=read(output/'summary.json')
    check(summary['primary_events']==len(pairs) and summary['strict_collector_matured_events']==0,'event_count_summary')
    check(summary['decision']=='NEEDS_MORE_EVIDENCE' and not summary['validated_edge'] and not summary['live_eligible'],'decision_gate')
    changed=subprocess.check_output(['git','diff','--name-status',PARENT,'HEAD'],cwd=ROOT,text=True).splitlines()
    check(all(line.startswith('A\t') for line in changed),'parent_files_modified')
    review=dict(status='PASS',review_type='same_author_separate_process_not_external_audit',
        raw_successful_receipts=len(valid),raw_unique_content_hashes=len(unique),chain_proofs_rechecked=proofs,
        candidate_events=len(events),original_chain_text_and_ipfs_hashes_verified=True,
        independently_matched_events=len(pairs),unique_source_price_paths_recomputed=len(selected),
        all_473267_atlas_rows_scanned=True,source_archive_markets=len(h),source_archive_bars=sum(map(len,h.values())),
        prior_tracked_files_unchanged=True,live_eligibility=False,
        limitations=['Explorer agreement, not local consensus/Merkle proof.','Selected sparse cohort and retrospectively retrieved sources.',
                     'No new strategy or account ledger; inherited PR7 accounts and rejections preserved.'])
    save_json(output/'independent_review.json',review);print(json.dumps(review),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__)
    for flag in ('evidence','output','atlas','archive'):p.add_argument('--'+flag,type=Path,required=True)
    a=p.parse_args();audit(a.evidence,a.output,a.atlas,a.archive)
