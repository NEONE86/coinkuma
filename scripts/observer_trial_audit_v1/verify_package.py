"""Verify the evidence manifest and recompute the actual trial without network."""
import argparse
import hashlib
import json
from pathlib import Path
from verify_trial import verify

def main(root):
    root=root.resolve()
    manifest=json.loads((root/'manifest.json').read_text(encoding='utf-8'))
    actual={p.relative_to(root).as_posix() for p in root.rglob('*') if p.is_file() and p!=root/'manifest.json'}
    assert actual==set(manifest),'PACKAGE_FILE_SET'
    for name,value in manifest.items():
        file=(root/name).resolve();assert file.is_relative_to(root),'PATH_ESCAPE'
        raw=file.read_bytes()
        assert hashlib.sha256(raw).hexdigest()==value['sha256'] and len(raw)==value['bytes'],'PACKAGE_FILE_BYTES:'+name
    recomputed=verify(root)
    assert recomputed==json.loads((root/'measurements.json').read_text(encoding='utf-8')),'MEASUREMENT_RECOMPUTATION'
    status=json.loads((root/'audit_status.json').read_text(encoding='utf-8'))
    assert status['independent_audit']=='PASS' and not status['automatic_repeat_enabled']
    released=sum(r['count'] for r in recomputed['pending']['outcomes'] if r['state']=='RELEASED')
    assert status['pending_released']==released and status['observed_receipts']==sum(recomputed['receipt_counts'].values()),'AUDIT_STATUS_COUNTS'
    assert status['paper_decisions']==recomputed['paper']['decisions'] and status['paper_fills']==recomputed['paper']['fills'],'AUDIT_PAPER_COUNTS'
    return dict(status='PASS_EXTRACTED_REAL_TRIAL_BUNDLE',files=len(manifest),commit=recomputed['commit'],source_hash=recomputed['source_hash'],
        ledger_records=recomputed['independent_verifier']['records'],receipts=sum(recomputed['receipt_counts'].values()),
        known_application_bytes=recomputed['meter']['known_received_application_bytes'],pending_released=released,paper_fills=recomputed['paper']['fills'],
        actual_model_calls=recomputed['limits']['actual_models'],actual_orders=recomputed['limits']['actual_orders'],
        actual_notifications=recomputed['limits']['actual_notifications'],investment_edge_validated=False)

if __name__=='__main__':
    p=argparse.ArgumentParser();p.add_argument('bundle',type=Path);a=p.parse_args()
    print(json.dumps(main(a.bundle),indent=2))
