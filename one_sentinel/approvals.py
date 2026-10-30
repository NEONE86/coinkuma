"""Bind one explicit approval to one persistent runtime, outside its ledger.

Claims are never reset on restart or runtime deletion. They are local operational
guards, not a signature authority against an owner who can edit all local files.
"""
from contextlib import closing
import json
from pathlib import Path
import sqlite3
import uuid
from .store import digest, encode

def claim_store():
    # Stable for the operating account across runtime parents and code checkouts.
    # No approval field, CLI runtime flag or per-run environment override selects it.
    return Path.home()/'.one-observer'/'approval-claims.sqlite3'

def location(ledger,value):
    target=value.get('approved_runtime')
    if not isinstance(target,str) or not Path(target).is_absolute() or Path(target).resolve()!=ledger.root:
        raise ValueError('APPROVED_RUNTIME_REQUIRED')
    if not isinstance(value.get('run_id'),str) or not value['run_id'].strip():
        raise ValueError('APPROVED_RUN_ID_REQUIRED')
    return claim_store()

def claim_key(value):return digest(value['approval_reference'])

def expected(ledger,value,instance):
    return dict(id='v1',run_id=value['run_id'],approved_runtime=str(ledger.root),
                approval_hash=digest(value),ledger_instance=instance)

def bind(ledger,value):
    path=location(ledger,value);path.parent.mkdir(parents=True,exist_ok=True)
    with closing(sqlite3.connect(path,timeout=10)) as db:
        db.execute('CREATE TABLE IF NOT EXISTS claims(approval_key TEXT PRIMARY KEY,body TEXT NOT NULL)')
        db.execute('BEGIN IMMEDIATE')
        stored=db.execute('SELECT body FROM claims WHERE approval_key=?',(claim_key(value),)).fetchone()
        binding=ledger.get('approval_binding','v1')
        if stored:
            if not binding or json.loads(stored[0])!=binding or binding!=expected(ledger,value,binding['ledger_instance']) or ledger.get('trial_approval','v1')!=value:
                raise ValueError('APPROVAL_ALREADY_CONSUMED')
        else:
            binding=binding or expected(ledger,value,uuid.uuid4().hex)
            if binding!=expected(ledger,value,binding['ledger_instance']):raise ValueError('APPROVAL_BINDING_MISMATCH')
            # Ledger first: a crash before the external commit can safely resume
            # this same instance with its attempts intact, never a fresh budget.
            with ledger.transaction():
                ledger.add('approval_binding','v1',binding)
                ledger.add('trial_approval','v1',value)
            db.execute('INSERT INTO claims VALUES(?,?)',(claim_key(value),encode(binding)))
        db.commit()

def verify_binding(ledger,value):
    path=location(ledger,value);binding=ledger.get('approval_binding','v1')
    if not binding or binding!=expected(ledger,value,binding['ledger_instance']) or ledger.get('trial_approval','v1')!=value:
        raise ValueError('APPROVAL_BINDING_REQUIRED')
    if not path.is_file():raise ValueError('APPROVAL_CLAIM_MISSING')
    with closing(sqlite3.connect(path.resolve().as_uri()+'?mode=ro',uri=True)) as db:
        row=db.execute('SELECT body FROM claims WHERE approval_key=?',(claim_key(value),)).fetchone()
    if not row or json.loads(row[0])!=binding:raise ValueError('APPROVAL_CLAIM_MISMATCH')
