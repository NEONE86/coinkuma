"""Public-code boundary and allowlisted, digest-bound private evidence pointers.

No private data is downloaded. Remote visibility/upload verification belongs to
the authorized publication step; a public CI job must never receive its token.
"""
import argparse
import json
from pathlib import Path
import re
import subprocess
from urllib.parse import urlsplit

ROOT=Path(__file__).resolve().parents[1]
FIELDS={'schema','run_id','classification','executed_commit','source_sha256','archive_sha256',
        'archive_bytes','audit_status','storage_status','private_repository','private_url',
        'private_asset_id','remote_digest_verified','public_raw_data','limitations'}

def validate_reference(row,require_stored=False):
    if set(row)!=FIELDS:raise ValueError('REFERENCE_FIELDS')
    if row['schema']!='ONE_OBSERVER_EVIDENCE_REFERENCE_V1':raise ValueError('SCHEMA')
    if not isinstance(row['run_id'],str) or not re.fullmatch(r'[A-Z0-9_-]{8,100}',row['run_id']):raise ValueError('RUN_ID')
    if row['classification']!='REAL_FORWARD':raise ValueError('CLASSIFICATION')
    for key,n in [('executed_commit',40),('source_sha256',64),('archive_sha256',64)]:
        if not isinstance(row[key],str) or not re.fullmatch('[0-9a-f]{'+str(n)+'}',row[key]):raise ValueError('HASH:'+key)
    if type(row['archive_bytes']) is not int or row['archive_bytes']<=0:raise ValueError('ARCHIVE_SIZE')
    if row['audit_status'] not in ('PASS_REAL_PUBLIC_TRIAL_AUDIT','FAILED','INCOMPLETE'):raise ValueError('AUDIT_STATUS')
    if row['public_raw_data'] is not False:raise ValueError('PUBLIC_RAW_FORBIDDEN')
    if row['private_repository']!='NEONE86/coinkuma-observer-evidence':raise ValueError('PRIVATE_DESTINATION')
    if not isinstance(row['limitations'],list) or not all(type(v) is str and len(v)<200 for v in row['limitations']):raise ValueError('LIMITATIONS')
    if row['storage_status']=='AWAITING_PRIVATE_STORAGE':
        if row['private_url'] is not None or row['private_asset_id'] is not None or row['remote_digest_verified'] is not False:raise ValueError('FALSE_UPLOAD_CLAIM')
        if require_stored:raise ValueError('PRIVATE_EVIDENCE_NOT_STORED')
    elif row['storage_status']=='STORED_PRIVATE':
        if not isinstance(row['private_url'],str):raise ValueError('PRIVATE_LINK')
        u=urlsplit(row['private_url'])
        if u.scheme!='https' or u.netloc!='github.com' or not u.path.startswith('/'+row['private_repository']+'/releases/') or u.query or u.fragment:raise ValueError('PRIVATE_LINK')
        if type(row['private_asset_id']) is not int or row['private_asset_id']<=0 or row['remote_digest_verified'] is not True:raise ValueError('UPLOAD_NOT_VERIFIED')
    else:raise ValueError('STORAGE_STATUS')

def check_files(files):
    rejected=[]
    for name,raw in files:
        p=Path(name);parts={s.lower() for s in p.parts}
        if p.suffix.lower() in ('.zip','.bin','.sqlite','.sqlite3','.db','.bundle') or parts & {'raw','runtime','private_evidence','observer-runtime'} or p.name in ('ledger.jsonl','execution_approval.json','approval.final.json'):
            rejected.append(name);continue
        # Collected receipt/ledger JSON must not be disguised as a fixture.
        if p.suffix.lower() in ('.json','.jsonl'):
            if p.parent.as_posix()=='docs/observer/runs' and p.suffix=='.json':
                validate_reference(json.loads(raw))
            elif b'REAL_FORWARD' in raw:
                rejected.append(name)
    if rejected:raise ValueError('PRIVATE_PAYLOAD_IN_PUBLIC_GIT:'+','.join(rejected))

def main(root,require_stored=False):
    names=subprocess.check_output(['git','-C',str(root),'ls-files','-z'],text=True).split('\0')
    files=[(n,(root/n).read_bytes()) for n in names if n and (root/n).is_file()]
    check_files(files)
    refs=sorted((root/'docs/observer/runs').glob('*.json'))
    if not refs:raise ValueError('EVIDENCE_REFERENCE_MISSING')
    for file in refs:validate_reference(json.loads(file.read_text(encoding='utf-8')),require_stored)
    print(json.dumps(dict(status='PASS',tracked_files=len(files),references=len(refs),require_stored=require_stored,
                         private_data_downloaded=False,scope='PUBLIC_REFERENCE_AND_TRACKED_PAYLOAD_CHECK_ONLY')))

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('--require-stored',action='store_true');a=p.parse_args();main(ROOT,a.require_stored)
