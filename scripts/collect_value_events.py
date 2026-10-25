"""Collect bounded public evidence with no account, trading or paid API access."""
from __future__ import annotations
import argparse
import json
from pathlib import Path
import re
import sys
from urllib.parse import urlencode
ROOT=Path(__file__).resolve().parents[1]
sys.path.insert(0,str(ROOT))
from one_trading.value_evidence import EvidenceStore, save_json, uni_created, sha, base58, unixfs_cid

UNI={
 'alpha0':'0x5e4be8Bc9637f0EAA1A755019e06A68ce081D58F',
 'alpha1':'0xC4e172459f1E7939D522503B81AFAaC1014CE6F6',
 'bravo':'0x408ED6354d4973f66138C91495F2f2FCbd8724C3'}
AAVE='0x9AEE0B04504CeF83A65AC3f0e838D0593BCb2BC7'
PAYLOADS='0xdAbad81aF85554E9ae636395611C58F7eC1aAEc5'
TOPICS={
 'uni_created':'0x7d84a6263ae0d98d3329bd7b46bb4e8d6f98cd35a7adb45c274c8b7fd5ebd5e0',
 'executed':'0x712ae1383f79ac853f8d882153778e0260ef8f03b504e2866e0593e04d2b291f',
 'uni_queued':'0x9a2e42fd6722813d69113e7d0079d3d940171428df7373df9c7f7617cfda2892',
 'canceled':'0x789cf55be980739dad1d0699b93b58e806b51c9d96619bfa8fe0a28abaa7b30c',
 'aave_created':'0xcc914becfa276bbc067049bf8db2d34ebbdc1bafa851e4d4936aaed376c08dbe',
 'aave_queued':'0xe39e7fc9f2013b8ab01110f66610f9fb8675d3126e69b3752f0084afc72be19a',
 'payload_sent':'0x9c687134d572ff69f3e23f40470562650574c5c7cab9f535268763b2d00da65b',
 'payload_executed':'0xda6084bb0aa902a7f6da10ba185d4aa129414651c90772417eff02a52112af2a',
 'payload_created':'0x1e4588da4731f84a598f061ee45829a6450aa00aa28962657b6835641afbbac5',
 'payload_queued':'0x1c4585f98a2bf1a1dca30917ba110f1b978343c7cce2c703495e5e47d5164456'}
UNI_PATTERN=re.compile(r'protocol fee|fee switch|burn.{0,40}uni|uni.{0,40}burn',re.I|re.S)
AAVE_PATTERN=re.compile(r'buyback|aavenomic|financecommittee',re.I)


def logs(store,address,topic):
    params={'topic':topic}; out=[]; cursors=set(); pages=[]
    for _ in range(200):
        url='https://eth.blockscout.com/api/v2/addresses/'+address+'/logs?'+urlencode(params)
        if url in cursors:
            raise ValueError('pagination_cycle')
        cursors.add(url)
        j,s=store.json(url); pages.append(s['sha256'])
        for row in j['items']:
            if row['address']['hash'].lower()!=address.lower() or row['topics'][0]!=topic:
                raise ValueError('log_filter_mismatch')
            row=dict(row,source_url=url,source_sha256=s['sha256'],first_seen_at=s['first_seen_at'])
            out.append(row)
        params=j['next_page_params']
        if params is None:
            break
        if params.get('topic')!=topic:
            raise ValueError('pagination_topic_lost')
    else:
        raise ValueError('pagination_limit')
    ids=[(r['transaction_hash'],r['index']) for r in out]
    if len(set(ids))!=len(ids):
        raise ValueError('duplicate_logs')
    print(json.dumps(dict(phase='LOGS',address=address,topic=topic,count=len(out),pages=len(pages))),flush=True)
    return out


def collect(root,offline=False):
    store=EvidenceStore(root,offline=offline)
    data={'uni':{},'aave':{},'payloads':{},'aave_paths':[], 'aave_documents':[],'errors':[]}
    for name,address in UNI.items():
        data['uni'][name]={k:logs(store,address,TOPICS[k]) for k in ('uni_created','executed','uni_queued','canceled')}
        for r in data['uni'][name]['uni_created']:
            pid,text=uni_created(r['data'])
            try:
                description=text.decode('utf-8'); valid_utf8=True
            except UnicodeDecodeError:
                description=text.decode('utf-8',errors='replace'); valid_utf8=False
            r.update(proposal_id=pid,description_sha256=sha(text),description=description,valid_utf8=valid_utf8,
                     candidate=bool(UNI_PATTERN.search(description)))
    save_json(Path(root)/'inventory.json',data)
    for k in ('aave_created','executed','aave_queued','canceled','payload_sent'):
        data['aave'][k]=logs(store,AAVE,TOPICS[k])
    for k in ('payload_created','payload_queued','payload_executed'):
        data['payloads'][k]=logs(store,PAYLOADS,TOPICS[k])
    save_json(Path(root)/'inventory.json',data)
    # Historical year-end trees avoid restricting the source inventory to files
    # still present today. Git dates are locators, NOT availability proof.
    trees=[]
    for end in ('2023-12-31T23:59:59Z','2024-12-31T23:59:59Z','2025-12-31T23:59:59Z','2026-09-30T23:59:59Z'):
        commits,_=store.json('https://api.github.com/repos/aave-dao/aave-proposals-v3/commits?'+urlencode({'until':end,'per_page':1}))
        if not commits:
            continue
        commit=commits[0]['sha']; tree,rec=store.json('https://api.github.com/repos/aave-dao/aave-proposals-v3/git/trees/'+commit+'?recursive=1')
        if tree.get('truncated'):
            raise ValueError('github_tree_truncated')
        trees.append(dict(until=end,commit=commit,source_sha256=rec['sha256']))
        for item in tree['tree']:
            if AAVE_PATTERN.search(item['path']):
                data['aave_paths'].append(dict(commit=commit,**item))
    # Executed proposal source folders are routinely removed. Resolve every
    # retained matching diff's introduction commit to recover its source tree;
    # don't let current file survival decide which events are observed.
    diff_paths=sorted({r['path'] for r in data['aave_paths'] if r['path'].startswith('diffs/')})
    for path in diff_paths:
        commits,_=store.json('https://api.github.com/repos/aave-dao/aave-proposals-v3/commits?'+urlencode({'path':path,'per_page':100}))
        if len(commits)==100:
            raise ValueError('unpaginated_source_history')
        for commit_row in commits[-1:]:
            commit=commit_row['sha']
            tree,rec=store.json('https://api.github.com/repos/aave-dao/aave-proposals-v3/git/trees/'+commit+'?recursive=1')
            if tree.get('truncated'):
                raise ValueError('github_tree_truncated')
            trees.append(dict(diff_origin=path,commit=commit,source_sha256=rec['sha256']))
            for item in tree['tree']:
                if AAVE_PATTERN.search(item['path']):
                    data['aave_paths'].append(dict(commit=commit,**item))
    seen=set()
    for row in data['aave_paths']:
        path=row['path']
        if row['type']!='blob' or not path.endswith(('.md','.s.sol','.json')) or path.startswith('diffs/') or row['sha'] in seen:
            continue
        seen.add(row['sha'])
        url='https://raw.githubusercontent.com/aave-dao/aave-proposals-v3/'+row['commit']+'/'+path
        b,s=store.get(url)
        data['aave_documents'].append(dict(path=path,commit=row['commit'],url=url,sha256=s['sha256'],
            first_seen_at=s['first_seen_at'],text=b.decode('utf-8'),unixfs_cid=unixfs_cid(b) if len(b)<=262144 else None))
    data['aave_trees']=trees
    for height in (12964999,12965000):
        data['london_'+str(height)]=store.json('https://eth.blockscout.com/api/v2/blocks/'+str(height))[0]
    sources=[
      'https://developers.uniswap.org/docs/ecosystem/governance/technical-reference',
      'https://raw.githubusercontent.com/Uniswap/governance/master/contracts/GovernorAlpha.sol',
      'https://raw.githubusercontent.com/bgd-labs/aave-address-book/main/src/GovernanceV3Ethereum.sol',
      'https://blog.ethereum.org/2021/07/15/london-mainnet-announcement',
      'https://eips.ethereum.org/EIPS/eip-1559',
      'https://blog.uniswap.org/unification',
      'https://governance.aave.com/t/arfc-aavenomics-implementation-part-one/21248.json',
      'https://governance.aave.com/t/aave-dao-funding-insights/24192.json',
      'https://governance.aave.com/t/arfc-pause-aave-buybacks/24686.json']
    for url in sources:
        try:
            store.get(url)
        except Exception as e:
            data['errors'].append(dict(url=url,error=str(e)))
    save_json(Path(root)/'inventory.json',data)
    print(json.dumps(dict(phase='COLLECTED',uni_candidates=[(name,r['proposal_id'],r['description'][:100]) for name,d in data['uni'].items() for r in d['uni_created'] if r['candidate']],
        aave_documents=[r['path'] for r in data['aave_documents']],source_errors=data['errors'])),flush=True)


if __name__=='__main__':
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--output',type=Path,required=True);p.add_argument('--offline',action='store_true')
    args=p.parse_args();collect(args.output,args.offline)
