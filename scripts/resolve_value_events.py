"""Resolve source bytes and chain execution separately, before any price join."""
from __future__ import annotations
import argparse
from datetime import datetime, timezone
import json
from pathlib import Path
import re
import sys
ROOT=Path(__file__).resolve().parents[1];sys.path.insert(0,str(ROOT))
from one_trading.value_evidence import EvidenceStore,base58,unixfs_cid,save_json,timestamp,iso,sha,word,uni_created
from scripts.collect_value_events import UNI, AAVE, PAYLOADS


def resolve(root,offline=False):
    root=Path(root);store=EvidenceStore(root,offline=offline)
    inventory=json.loads((root/'inventory.json').read_text(encoding='utf-8'))
    titles={}
    for d in inventory['aave_documents']:
        if not d['path'].endswith('.md'):continue
        title=re.search(r'^title:\s*[\"\']?(.*?)[\"\']?\s*$',d['text'],re.M).group(1)
        date=re.search(r'src/(\d{8})',d['path']).group(1)
        dt=datetime.strptime(date,'%Y%m%d').replace(tzinfo=timezone.utc)
        titles[title.lower()]=int(dt.timestamp()*1000)
    result={'ipfs':[],'errors':[],'candidate_titles':titles,'lookup_window_days':[-1,14]}
    for log in inventory['aave']['aave_created']:
        day=timestamp(log['block_timestamp'])
        if not any(t-86400000<=day<t+15*86400000 for t in titles.values()):continue
        cid=base58(b'\x12\x20'+bytes.fromhex(log['data'].removeprefix('0x')))
        url='https://gateway.pinata.cloud/ipfs/'+cid
        try:
            b,rec=store.get(url)
            if unixfs_cid(b)!=cid:raise ValueError('ipfs_cid_mismatch')
            text=b.decode('utf-8')
            m=re.search(r'^title:\s*[\"\']?(.*?)[\"\']?\s*$',text,re.M)
            title=m.group(1) if m else ''
            result['ipfs'].append(dict(proposal_id=int(log['topics'][1],16),cid=cid,title=title,
                candidate=title.lower() in titles,source_url=url,source_sha256=rec['sha256'],
                first_seen_at=rec['first_seen_at'],text=text))
            print(json.dumps(dict(phase='IPFS_VERIFIED',id=int(log['topics'][1],16),title=title,selected=title.lower() in titles)),flush=True)
        except Exception as e:
            result['errors'].append(dict(cid=cid,proposal_id=int(log['topics'][1],16),error=str(e)))
    save_json(root/'ipfs_resolution.json',result)


def pid(log,indexed=False):
    return int(log['topics'][1],16) if indexed else word(bytes.fromhex(log['data'][2:]),0)


def proof(store,log):
    """Cross-provider raw receipt comparison, block hash/time and exact ABI log.

    Explorer agreement is reported honestly; it is not a locally verified
    consensus/Merkle receipt inclusion proof. All receipts are retained.
    """
    height=log['block_number'];txhash=log['transaction_hash']
    block_url='https://eth.blockscout.com/api/v2/blocks/'+str(height)
    block,bs=store.json(block_url)
    tx_url='https://eth.blockscout.com/api/v2/transactions/'+txhash
    tx,ts=store.json(tx_url)
    if block['hash'].lower()!=log['block_hash'].lower() or timestamp(block['timestamp'])!=timestamp(log['block_timestamp']):
        raise ValueError('block_identity_mismatch')
    if tx['status']!='ok' or tx['block_number']!=height or tx['hash'].lower()!=txhash.lower() or timestamp(tx['timestamp'])!=timestamp(log['block_timestamp']):
        raise ValueError('transaction_status_or_time_mismatch')
    url='https://api.routescan.io/v2/network/mainnet/evm/1/etherscan/api?module=proxy&action=eth_getTransactionReceipt&txhash='+txhash
    receipt,rs=store.json(url); receipt=receipt.get('result')
    if not isinstance(receipt,dict) or receipt.get('status')!='0x1' or receipt['transactionHash'].lower()!=txhash.lower() or int(receipt['blockNumber'],16)!=height or receipt['blockHash'].lower()!=block['hash'].lower():
        raise ValueError('independent_receipt_mismatch')
    matches=[r for r in receipt['logs'] if int(r['logIndex'],16)==log['index']]
    if len(matches)!=1:raise ValueError('receipt_log_missing')
    match=matches[0]
    if match.get('removed',False) or match['address'].lower()!=log['address']['hash'].lower() or match['data'].lower()!=log['data'].lower() or [x.lower() for x in match['topics']]!=[x.lower() for x in log['topics'] if x]:
        raise ValueError('independent_receipt_log_mismatch')
    block2url='https://api.routescan.io/v2/network/mainnet/evm/1/etherscan/api?module=block&action=getblockreward&blockno='+str(height)
    block2,b2s=store.json(block2url)
    if int(block2['result']['timeStamp'])*1000!=timestamp(block['timestamp']):
        raise ValueError('independent_block_time_mismatch')
    records=[store.sources[log['source_url']],bs,ts,rs,b2s]
    return dict(block_number=height,block_hash=block['hash'],transaction_hash=txhash,log_index=log['index'],
        published_at=block['timestamp'],sources=[dict(url=r['url'],sha256=r['sha256'],first_seen_at=r['first_seen_at']) for r in records],
        evidence_complete_at=max(r['first_seen_at'] for r in records),cross_provider_exact_receipt_match=True,
        consensus_or_merkle_proof_verified=False)


def build(root,classification,offline=False):
    root=Path(root);store=EvidenceStore(root,offline=offline)
    inv=json.loads((root/'inventory.json').read_text(encoding='utf-8'))
    resolution=json.loads((root/'ipfs_resolution.json').read_text(encoding='utf-8'))
    review=json.loads(Path(classification).read_text(encoding='utf-8'))
    events=[];errors=[]
    for name,group in inv['uni'].items():
        for log in group['uni_created']:
            if not log['candidate']:continue
            id=log['proposal_id']; key='UNI-'+name+'-'+str(id); annotation=review[key]
            e=dict(event_id=key,symbol='UNIUSDT',proposal_id=id,governor=UNI[name],chain_id=1,
                title=log['description'].strip().splitlines()[0].lstrip('# '),
                source_text_sha256=log['description_sha256'],source_envelope_sha256=log['source_sha256'],
                source_url=log['source_url'],first_seen_at=log['first_seen_at'],historical_collector_first_seen_at=None,
                source_time_proven=False,published_at=None,executed_at=None,**annotation)
            if not log['valid_utf8']:
                e['timing_status']='QUALITATIVE_ONLY_INVALID_UTF8'
            else:
                try:
                    e['publication_proof']=proof(store,log)
                    e.update(source_time_proven=True,published_at=log['block_timestamp'],
                             evidence_complete_at=e['publication_proof']['evidence_complete_at'],timing_status='CHAIN_RECONSTRUCTED')
                except Exception as ex:
                    e['timing_status']='QUALITATIVE_ONLY_PROOF_INCOMPLETE';errors.append(dict(event_id=key,phase='publication',error=str(ex)))
            e['governance_executed_at']=None;e['canceled_at']=None;e['queued_at']=None
            for kind,field in [('executed','governance_executed_at'),('canceled','canceled_at'),('uni_queued','queued_at')]:
                found=[r for r in group[kind] if pid(r)==id]
                if found:
                    try:
                        p=proof(store,found[0]);e[field]=found[0]['block_timestamp'];e[field+'_proof']=p
                        if kind=='executed' and e.get('execution_scope')=='ETHEREUM_MAINNET':
                            e['executed_at']=e[field]
                    except Exception as ex:
                        errors.append(dict(event_id=key,phase=kind,error=str(ex)))
            # Unknown downstream chains are never assigned the L1 dispatch time.
            if key=='UNI-bravo-93' and e['executed_at']:
                p=e['governance_executed_at_proof']
                u=next(s['url'] for s in p['sources'] if 'action=eth_getTransactionReceipt' in s['url'])
                receipt=store.json(u)[0]['result']
                transfers=[r for r in receipt['logs'] if r['address'].lower()=='0x1f9840a85d5af5bf1d1762f925bdaddc4201f984'
                    and r['topics'][0]=='0xddf252ad1be2c89b69c2b068fc378daa952ba7f163c4a11628f55a4df523b3ef'
                    and int(r['topics'][2],16)==0xdead and int(r['data'],16)==100000000*10**18]
                if len(transfers)!=1:raise ValueError('unification_sink_transfer_not_verified')
                e['economic_execution_evidence']=dict(kind='UNI_TRANSFER_TO_DEAD',amount_base_units=str(100000000*10**18),
                    amount_tokens=100000000,receipt_url=u,log_index=int(transfers[0]['logIndex'],16),
                    total_supply_decrease_proven=False,ongoing_fee_revenue_quantified=False)
            events.append(e)
            print(json.dumps(dict(phase='EVENT_PROOF',event_id=key,status=e['timing_status'])),flush=True)
    for ipfs in resolution['ipfs']:
        if not ipfs['candidate']:continue
        id=ipfs['proposal_id'];key='AAVE-v3-'+str(id)
        log=next(r for r in inv['aave']['aave_created'] if pid(r,True)==id)
        e=dict(event_id=key,symbol='AAVEUSDT',proposal_id=id,governor=AAVE,chain_id=1,title=ipfs['title'],
            source_url=ipfs['source_url'],source_text_sha256=ipfs['source_sha256'],source_envelope_sha256=log['source_sha256'],
            ipfs_cid=ipfs['cid'],ipfs_verified=True,first_seen_at=ipfs['first_seen_at'],historical_collector_first_seen_at=None,
            direct_value_accrual=True,mechanism='BUY_AND_DISTRIBUTE_ALLOWANCE',direction='ENABLE',
            classification_reason='Original CID text authorizes 4m aEthUSDT allowance to AFC for market purchases and reserve distribution; not burn, not completed purchases.',
            source_time_proven=False,published_at=None,executed_at=None,execution_scope='ETHEREUM_MAINNET_ALLOWANCE',
            realized_buyback_at=None,realized_buyback_amount=None)
        try:
            e['publication_proof']=proof(store,log)
            e.update(source_time_proven=True,published_at=log['block_timestamp'],
                evidence_complete_at=max(ipfs['first_seen_at'],e['publication_proof']['evidence_complete_at']),timing_status='CHAIN_RECONSTRUCTED')
        except Exception as ex:
            e['timing_status']='QUALITATIVE_ONLY_PROOF_INCOMPLETE';errors.append(dict(event_id=key,phase='publication',error=str(ex)))
        e['governance_executed_at']=None;e['canceled_at']=None;e['queued_at']=None
        for kind,field in [('executed','governance_executed_at'),('canceled','canceled_at'),('aave_queued','queued_at')]:
            found=[r for r in inv['aave'][kind] if pid(r,True)==id]
            if found:
                try:
                    e[field+'_proof']=proof(store,found[0]);e[field]=found[0]['block_timestamp']
                except Exception as ex:errors.append(dict(event_id=key,phase=kind,error=str(ex)))
        sent=[r for r in inv['aave']['payload_sent'] if pid(r,True)==id and int(r['topics'][3],16)==1 and ('0x'+r['topics'][2][-40:]).lower()==PAYLOADS.lower()]
        e['payloads']=[]
        for r in sent:
            payload_id=word(bytes.fromhex(r['data'][2:]),0)
            payload=dict(id=payload_id,sent_proof=proof(store,r))
            executed=[x for x in inv['payloads']['payload_executed'] if pid(x)==payload_id]
            if executed:
                payload['execution_proof']=proof(store,executed[0]);payload['executed_at']=executed[0]['block_timestamp']
            e['payloads'].append(payload)
        if e['payloads'] and all(p.get('executed_at') for p in e['payloads']):
            e['executed_at']=max(p['executed_at'] for p in e['payloads'])
            p=e['payloads'][0]['execution_proof']
            u=next(s['url'] for s in p['sources'] if 'action=eth_getTransactionReceipt' in s['url'])
            receipt=store.json(u)[0]['result']
            approvals=[r for r in receipt['logs'] if r['address'].lower()=='0x23878914efe38d27c4d67ab83ed1b93a74d4086a'
                and r['topics'][0]=='0x8c5be1e5ebec7d5bd14f71427d1e84f3dd0314c0f7b2291e5b200ac8c7c3b925'
                and r['topics'][1][-40:].lower()=='464c71f6c2f760dda6093dcb91c24c39e5d6e18c'
                and r['topics'][2][-40:].lower()=='22740deba78d5a0c24c58c740e3715ec29de1bfa'
                and int(r['data'],16)==4000000*10**6]
            if len(approvals)!=1:raise ValueError('aave_allowance_execution_not_verified')
            e['economic_execution_evidence']=dict(kind='AETHUSDT_ALLOWANCE',amount_base_units=str(4000000*10**6),
                receipt_url=u,log_index=int(approvals[0]['logIndex'],16),completed_buyback_proven=False)
        events.append(e)
        print(json.dumps(dict(phase='EVENT_PROOF',event_id=key,status=e['timing_status'])),flush=True)
    b,s=store.json('https://eth.blockscout.com/api/v2/blocks/12965000')
    prev,ps=store.json('https://eth.blockscout.com/api/v2/blocks/12964999')
    u='https://api.routescan.io/v2/network/mainnet/evm/1/etherscan/api?module=block&action=getblockreward&blockno=12965000'
    other,os=store.json(u)
    proven=(prev['hash']==b['parent_hash'] and b['base_fee_per_gas']=='1000000000'
        and prev['base_fee_per_gas'] is None and int(other['result']['timeStamp'])*1000==timestamp(b['timestamp']))
    events.append(dict(event_id='ETH-london-12965000',symbol='ETHUSDT',chain_id=1,title='London base-fee burn activation',
        source_url=s['url'],source_text_sha256=s['sha256'],source_envelope_sha256=s['sha256'],
        published_at=b['timestamp'] if proven else None,executed_at=b['timestamp'] if proven else None,
        first_seen_at=s['first_seen_at'],evidence_complete_at=max(s['first_seen_at'],os['first_seen_at'],ps['first_seen_at']),
        historical_collector_first_seen_at=None,direct_value_accrual=True,source_time_proven=proven,
        mechanism='BASE_FEE_BURN_ACTIVATION',direction='ENABLE',execution_scope='ETHEREUM_MAINNET',
        classification_reason='Execution-stage base-fee burn observation only; no proven earlier announcement vintage or claim of net deflation.',
        timing_status='CHAIN_RECONSTRUCTED' if proven else 'QUALITATIVE_ONLY_PROOF_INCOMPLETE',
        publication_proof=dict(block_number=b['height'],block_hash=b['hash'],published_at=b['timestamp'],
            sources=[dict(url=r['url'],sha256=r['sha256'],first_seen_at=r['first_seen_at']) for r in (s,os,ps)],
            cross_provider_block_timestamp_match=proven,consensus_or_merkle_proof_verified=False)))
    save_json(root/'event_panel.json',events);save_json(root/'proof_errors.json',errors)
    print(json.dumps(dict(phase='PANEL_BUILT',events=len(events),errors=errors)),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--evidence',type=Path,required=True);p.add_argument('--classifications',type=Path);p.add_argument('--offline',action='store_true')
    a=p.parse_args()
    if a.classifications:build(a.evidence,a.classifications,a.offline)
    else:resolve(a.evidence,a.offline)
