"""Independent stdlib checks for append-only release/retry evidence.

No one_sentinel imports. Called sequentially after raw/initial clock checks.
This proves internal consistency, not external authenticity of a local timestamp.
"""
import collections
import hashlib
import json
from decimal import Decimal


def digest(x):
    return hashlib.sha256(json.dumps(x,sort_keys=True,ensure_ascii=False,separators=(',',':'),allow_nan=False).encode()).hexdigest()


class Audit:
    def __init__(self):
        self.active={};self.generations=collections.Counter();self.opened={}

    def check(self,kind,key,b,index,contract,positions,expected_time):
        policy=contract['policy'];cfg=policy['clock_model']
        if kind=='clock_pending':
            assert ('interpretation',b['initial_interpretation_id']) in index,'PENDING_INITIAL_MISSING'
            initial=index['interpretation',b['initial_interpretation_id']]
            r=index['receipt',b['receipt_id']];item=initial['items'][b['item_index']]
            assert initial['receipt_id']==r['id']==b['initial_interpretation_id'],'PENDING_INITIAL_BINDING'
            assert r['classification']!='REAL_REUSED' and item['state']=='UNCERTAIN' and 'lower' in item,'PENDING_INELIGIBLE'
            assert b['timing']==r['timing'] and b['source']==r['source'] and b['symbol']==item['symbol'] and b['source_time']==item['source_time'],'PENDING_RECEIPT_BINDING'
            assert b['calibration_id']==initial['calibration_id'] and b['envelope_bytes']==r['bytes'],'PENDING_PROVENANCE'
            assert b['created_at']==initial['available_at'] and b['ttl_seconds']==policy['clock_pending']['ttl_seconds'],'PENDING_POLICY'
            assert key==digest(['BOUNDED_PENDING_RELEASE_V1',r['id'],b['item_index']]),'PENDING_ID'
            self.active[key]=b
        if kind=='interpretation_release':
            assert b['pending_id'] in self.active,'RELEASE_WITHOUT_PENDING'
            p=self.active[b['pending_id']];r=index['receipt',p['receipt_id']]
            initial=index['interpretation',p['initial_interpretation_id']]
            assert key==digest(['BOUNDED_PENDING_RELEASE_V1',p['id']]),'RELEASE_ID'
            assert all(b[f]==p[f] for f in ('receipt_id','initial_interpretation_id','item_index','symbol','source')),'RELEASE_BINDING'
            proof=b['proof_timing'];done=b['processing_timing'];original=p['timing']
            assert proof['session_id']==done['session_id']==original['session_id'],'RELEASE_EPOCH'
            assert b['available_at']==done['wall']>=proof['wall']>=initial['available_at']>=r['received_at'],'RELEASE_AVAILABILITY'
            elapsed=done['monotonic']-original['monotonic']
            assert 0<=proof['monotonic']-original['monotonic']<=elapsed<p['ttl_seconds'],'RELEASE_TTL'
            assert abs((done['wall']-proof['wall'])-(done['monotonic']-proof['monotonic']))<=cfg['wall_jump_seconds'],'RELEASE_PROCESSING_CLOCK'
            assert b['received_at']==r['received_at'] and b['wait_seconds']==done['wall']-r['received_at'],'RELEASE_WAIT'
            old=expected_time(p['source_time'],dict(r,timing=proof),index['clock_calibration',p['calibration_id']],cfg)
            assert 'lower' in old,'RELEASE_EXPIRED_OR_JUMPED_INITIAL_CALIBRATION'
            latest=next(v for (k,i),v in reversed(index.items()) if k=='clock_calibration')
            assert latest['id']==b['calibration_id'],'RELEASE_CALIBRATION'
            t=expected_time(p['source_time'],dict(r,timing=proof),latest,cfg)
            assert t['state']=='VALID','RELEASE_NOT_SAFE_PAST'
            if p['source']=='bar':
                first=initial['items'][p['item_index']]
                t['freshness_source_time']=first['freshness_source_time']
                t['age_upper']+=p['source_time']-t['freshness_source_time']
            assert b['time_validity']==t,'RELEASE_CLOCK_RECOMPUTATION'
            ttl=policy['price_fresh_seconds' if p['source']=='mini' else 'bar_fresh_seconds']
            assert t['age_upper']+done['monotonic']-proof['monotonic']<=ttl,'RELEASE_STALE'
            assert b['trigger'] in ('TICK','CALIBRATION'),'RELEASE_TRIGGER'
        if kind=='clock_pending_result':
            assert key in self.active,'PENDING_TERMINAL_DUPLICATE'
            p=self.active.pop(key)
            assert b['receipt_id']==p['receipt_id'] and b['symbol']==p['symbol'],'PENDING_RESULT_BINDING'
            if b['state']=='RELEASED':
                release=index['interpretation_release',b['release_id']]
                assert b['reason']=='SAFE_PAST_AND_FRESH' and b['at']==release['available_at'] and b['timing']==release['processing_timing'],'PENDING_RELEASE_RESULT'
            else:assert b['state']=='DROPPED' and not b['release_id'],'PENDING_DROP'
        if kind=='input' and b.get('release_id'):
            release=index['interpretation_release',b['release_id']];r=index['receipt',release['receipt_id']]
            assert key==release['id'] and b['receipt_id']==r['id'],'RELEASE_INPUT_ID'
            assert all(b[f]==r[f] for f in ('sha256','bytes','received_at','timing','processed_at','classification','origin','context','provenance')),'RELEASE_ORIGINAL_CHANGED'
            assert b['available_at']==release['available_at'] and b['validity_timing']==release['proof_timing'] and b['processing_timing']==release['processing_timing'],'RELEASE_INPUT_AVAILABILITY'
            assert b['time_validity']=={release['symbol'] if r['source']=='mini' else '@bar':release['time_validity']},'RELEASE_INPUT_CLOCK'
            if r['source']=='mini':assert set(b['parsed'])=={release['symbol']},'RELEASE_REPROCESSED_PEER'
        if kind=='decision_retry':
            parent=index['decision',b['parent_decision_id']];execution=index['execution',parent['id']];child=index['decision',b['decision_id']]
            assert digest(parent)==b['parent_decision_hash'] and digest(execution)==b['parent_execution_hash'],'RETRY_ORIGINAL_OVERWRITTEN'
            assert key==parent['id'] and execution['status']=='REJECTED' and execution['reason']=='NEW_MONOTONIC_EPOCH','RETRY_NOT_EPOCH_TERMINAL'
            s=parent['symbol'];assert child['symbol']==s and parent['action']==child['action']=='EXIT','RETRY_ACTION'
            assert positions[s]>0 and Decimal(child['quantity'])==positions[s],'RETRY_CURRENT_INVENTORY'
            assert child['parent_decision_id']==parent['id'] and child['attempt_generation']==b['generation'],'RETRY_LINK'
            assert child['timing']['session_id']==b['session_id']!=parent['timing']['session_id'],'RETRY_NEW_EPOCH'
            assert child['at']==b['at']>=execution['at'] and child['at']==child['timing']['wall'],'RETRY_AVAILABILITY'
            anchor=digest([s,self.opened[s]]);self.generations[anchor]+=1
            assert b['position_anchor']==anchor and b['generation']==self.generations[anchor]<=policy['epoch_exit_retry']['max_generations_per_position'],'RETRY_GENERATION'
            delay=policy['minimum_execution_delay_seconds'];ttl=policy['decision_ttl_seconds']
            assert delay>=2 and child['ready_at']==child['at']+delay and child['ready_monotonic']==child['timing']['monotonic']+delay,'RETRY_OWN_DELAY'
            assert child['expires_at']==child['at']+ttl and child['expires_monotonic']==child['timing']['monotonic']+ttl,'RETRY_OWN_TTL'
            reason=child['reason'];assert reason in policy['epoch_exit_retry']['eligible_reasons'],'RETRY_REASON'
            if reason=='STRUCTURAL_INVALIDATION':
                meta=next(index['input',i] for i in child['input_ids'] if index['input',i]['source']=='metadata')
                value=meta['parsed'].get(s)
                assert 0<=child['at']-meta['received_at']<=policy['metadata_fresh_seconds'] and (not value or value['status']!='TRADING' or not value['spot']),'RETRY_STRUCTURE_NO_LONGER_VALID'
            elif reason=='HORIZON_EXPIRED':assert child['at']>=self.opened[s]+policy['horizon_seconds'],'RETRY_HORIZON_NOT_EXPIRED'
        if kind=='fill' and b['side']=='BUY' and positions[b['symbol']]==0:self.opened[b['symbol']]=b['at']

    def finish(self,index,contract):
        cfg=contract['policy'].get('clock_pending')
        if not cfg:return
        assert len(self.active)<=cfg['max_items'],'PENDING_COUNT_CAP'
        assert sum({p['receipt_id']:p['envelope_bytes'] for p in self.active.values()}.values())<=cfg['max_envelope_bytes'],'PENDING_BYTE_CAP'
        for (kind,key),b in index.items():
            if kind=='interpretation_release':assert ('input',key) in index and ('clock_pending_result',b['pending_id']) in index,'RELEASE_ATOMICITY'
            if kind=='decision' and b.get('parent_decision_id'):assert index['decision_retry',b['parent_decision_id']]['decision_id']==key,'RETRY_ATOMICITY'
