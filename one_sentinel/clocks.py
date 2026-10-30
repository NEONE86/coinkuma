"""Source-clock intervals; never rewrite a wall receipt or infer a past calibration.

RTT bounds cover arbitrary path asymmetry. Drift and sample-age limits are frozen
engineering assumptions, not fitted to a price outcome or the observed 0.6s gap.
A UUID is new for every process: monotonic values are comparable only within it.
"""
import math
import time
import uuid

SESSION_ID=uuid.uuid4().hex
VERSION='SOURCE_INTERVAL_V1'

def capture(wall_clock=time.time, mono_clock=time.monotonic):
    before=mono_clock();wall=wall_clock();after=mono_clock()
    return dict(wall=wall,monotonic=after,session_id=SESSION_ID,
                capture_span=max(0,after-before),epoch_scope='PROCESS_SESSION_ONLY')

def valid_stamp(stamp):
    return bool(isinstance(stamp,dict) and stamp.get('session_id') and
                all(type(stamp.get(k)) in (int,float) and math.isfinite(stamp[k]) for k in ('wall','monotonic')))

def calibration(server_ms,sent,received,evidence_id,cfg):
    if type(server_ms) is not int or not valid_stamp(sent) or not valid_stamp(received):raise ValueError('INVALID_CLOCK_SAMPLE')
    if sent['session_id']!=received['session_id']:raise ValueError('CLOCK_EPOCH_MISMATCH')
    rtt=received['monotonic']-sent['monotonic']
    if not 0<=rtt<=cfg['max_rtt_seconds']:raise ValueError('CLOCK_RTT')
    wall_delta=received['wall']-sent['wall']
    if abs(wall_delta-rtt)>cfg['wall_jump_seconds']:raise ValueError('CLOCK_WALL_JUMP')
    return dict(id=evidence_id,evidence_id=evidence_id,version=VERSION,server_ms=server_ms,
                sent=sent,received=received,rtt_seconds=rtt,
                offset_lower=server_ms/1000-received['wall'],
                offset_upper=(server_ms+1)/1000-sent['wall'],
                bound_assumptions='server timestamp sampled during request/response; millisecond quantization; full RTT asymmetry; bounded drift')

def interval(stamp,sample,cfg):
    if not valid_stamp(stamp) or not sample:return dict(state='UNVERIFIABLE',reason='NO_CONTEMPORANEOUS_CALIBRATION')
    recv=sample['received'];sent=sample['sent']
    if stamp['session_id']!=recv['session_id']:return dict(state='UNVERIFIABLE',reason='NEW_MONOTONIC_EPOCH')
    age=stamp['monotonic']-recv['monotonic']
    if age<0:return dict(state='UNVERIFIABLE',reason='CALIBRATION_NOT_YET_AVAILABLE')
    if age>cfg['max_age_seconds']:return dict(state='UNCERTAIN',reason='CALIBRATION_EXPIRED')
    if abs((stamp['wall']-recv['wall'])-age)>cfg['wall_jump_seconds']+age*cfg['max_drift_ppm']/1e6:
        return dict(state='UNCERTAIN',reason='CLOCK_WALL_JUMP')
    drift=(age+sample['rtt_seconds'])*cfg['max_drift_ppm']/1e6
    drift+=stamp.get('capture_span',0)+recv.get('capture_span',0)+sent.get('capture_span',0)
    return dict(state='BOUNDED',lower=sample['server_ms']/1000+age-drift,
                upper=(sample['server_ms']+1)/1000+stamp['monotonic']-sent['monotonic']+drift,
                calibration_id=sample['id'],version=VERSION)

def assess(event_time,stamp,sample,cfg):
    bounds=interval(stamp,sample,cfg)
    if type(event_time) not in (int,float) or not math.isfinite(event_time) or event_time<0:
        return dict(state='INVALID',reason='INVALID_SOURCE_TIME',version=VERSION)
    if bounds['state']!='BOUNDED':return dict(bounds,version=VERSION)
    state='VALID' if event_time<=bounds['lower'] else ('FUTURE' if event_time>bounds['upper'] else 'UNCERTAIN')
    return dict(bounds,state=state,source_time=event_time,age_upper=max(0,bounds['upper']-event_time))

def fresh(row,source,symbol,now,max_age,current_stamp=None):
    if not row or now<row.get('available_at',row['received_at']):return False
    times=row.get('time_validity',{})
    item=times.get(symbol if source=='mini' else '@bar')
    if item is None:
        # Metadata/book have receipt freshness; old synthetic fixtures retain
        # their explicitly exact clock. Real price inputs require an assessment.
        if source in ('metadata','book'):return 0<=now-row['received_at']<=max_age
        return False
    if item['state']!='VALID':return False
    if item.get('version')=='SYNTHETIC_EXACT_V1':return 0<=now-item.get('freshness_source_time',item['source_time'])<=max_age
    old=row.get('validity_timing') or row.get('timing')
    if not valid_stamp(current_stamp) or not old or old['session_id']!=current_stamp['session_id']:return False
    elapsed=current_stamp['monotonic']-old['monotonic']
    # Detect local jumps again at decision time, not just arrival time.
    if elapsed<0 or abs((current_stamp['wall']-old['wall'])-elapsed)>.05:return False
    return item['age_upper']+elapsed<=max_age
