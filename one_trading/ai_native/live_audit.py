"""Independent source -> original model response -> PAPER/cost reconciliation.

No import of live runner, transport, planners or PAPER producer.
"""
import argparse
import base64
import hashlib
import json
from decimal import Decimal, ROUND_CEILING
from pathlib import Path

from .audit import verify as paper_verify, digest, at, check, approx
from .prospective_audit import verify as decision_verify


def read(path):
    return json.loads(path.read_text(encoding='utf-8'))


def verify(root):
    root = Path(root)
    config = read(root/'config.json')
    evidence = decision_verify(root/'decisions.jsonl', source_dir=root/'source')
    paper = read(root/'paper.json')
    recalculation = paper_verify(paper)
    rows = [json.loads(line) for line in (root/'decisions.jsonl').read_bytes().splitlines()]
    check(rows[0]['payload']['collector_commit'] == config['source_commit'], 'SOURCE_IDENTITY')
    persisted = [json.loads(line) for line in (root/'paper-events.jsonl').read_bytes().splitlines()]
    check(persisted == paper['ledger'], 'PAPER_JOURNAL_MISMATCH')
    frozen = {r['payload']['frame']['id']: r['payload']['frame'] for r in rows if r['kind']=='input_frozen'}
    receipts = [r['payload']['raw'] for r in paper['ledger'] if r['kind']=='receipt']
    check(receipts == list(frozen.values()), 'DIFFERENT_POLICY_INPUT_TIMELINE')
    source_times = {r['payload']['frame']['id']: {s: ref['received_at'] for s,ref in r['payload']['source_receipts'].items()}
                    for r in rows if r['kind']=='input_frozen'}
    for r in paper['ledger']:
        if r['kind']=='receipt':
            check(r['payload'].get('source_received_at')==source_times[r['payload']['id']], 'PAPER_SOURCE_RECEIPT_BINDING')
    epochs = [r['payload']['frame_id'] for r in rows if r['kind']=='decision_requested']
    for control in ('CASH', 'PRICE_ONLY'):
        decisions = [r['payload'] for r in paper['ledger']
                     if r['kind']=='decision' and r['payload']['arm']==control]
        check([d['source_frame'] for d in decisions]==epochs, 'CONTROL_TIMELINE')
        for d in decisions:
            c = d['context']; expected = ('HOLD','CASH',0.)
            if control=='PRICE_ONLY':
                moves = {s: h['current_mid']/h['previous_mid']-1
                         for s,h in c['history'].items() if h['previous_mid'] is not None}
                held = list(c['positions'])
                if held:
                    expected = ('SELL',held[0],1.) if moves.get(held[0],0)<=-.003 else ('HOLD',held[0],0.)
                elif moves:
                    symbol = max(moves, key=lambda s:(moves[s],s))
                    if moves[symbol]>=.003: expected = ('BUY',symbol,.1)
            p=d['plan']
            check((p['action'],p['symbol'],p['fraction'])==expected, 'CONTROL_RULE_CHANGED')
    requests = {r['payload']['request_id']: r for r in rows if r['kind']=='decision_requested'}
    responses = {r['payload']['request_id']: r for r in rows if r['kind']=='decision_received'}
    accepted = {r['payload']['request_id']: r for r in rows if r['kind']=='proposal_validated'}
    ai = {r['payload']['source_frame']: r['payload'] for r in paper['ledger']
          if r['kind']=='decision' and r['payload']['arm']=='AI'}
    charges = {r['payload']['source_frame']: r['payload']['amount'] for r in paper['ledger']
               if r['kind']=='model_cost_scheduled' and r['payload']['arm']=='AI'}
    check(len(ai)==len(accepted), 'HIDDEN_OR_FABRICATED_AI_DECISION')
    cost_events = {r['payload']['source_frame']: r['payload'] for r in paper['ledger'] if r['kind']=='model_cost_scheduled' and r['payload']['arm']=='AI'}
    proposals = {r['payload']['request_id']: r for r in rows if r['kind'] in ('proposal_validated','proposal_rejected')}
    expected_charges = {}
    for rid, row in requests.items():
        request = row['payload']; fid = request['frame_id']
        context = read(root/(rid+'-input.json'))
        check(context['frame_id']==fid and context['cutoff']==frozen[fid]['received_at'], 'MODEL_INPUT_IDENTITY')
        if rid not in responses:
            continue  # Explicit unresolved request; result below cannot be PASS.
        response = responses[rid]['payload']
        provider = read(root/(rid+'-provider.json'))
        check(provider['cost']==response['actual_cost_usdt'], 'PROVIDER_COST_MISMATCH')
        if config['mode']=='FREE_PROBE_NOT_AI':
            check(provider['kind']=='FREE_PROBE_NOT_AI' and provider['cost']=='0', 'FALSE_FREE_PROBE')
        else:
            check(provider['kind']=='OPENAI_USAGE_PRICED_NOT_INVOICE', 'PROVIDER_TYPE')
            env = json.loads(provider['provider_raw_utf8'])
            check(env==provider['provider_response'], 'PROVIDER_RAW_MISMATCH')
            check(json.loads(provider['request_body']['input'])==context, 'PROVIDER_INPUT_CHANGED')
            check(provider['request_body']['model']==config['model'], 'MODEL_CHANGED')
            check(digest(provider['request_body']['instructions'])==config['prompt_sha256'] and
                  provider['request_body']['tools']==[] and provider['request_body']['store'] is False,
                  'MODEL_PROMPT_OR_TOOLS_CHANGED')
            usage = env['usage']; rates = config['approval']['rates']
            i,c,o = usage['input_tokens'],usage['input_tokens_details']['cached_tokens'],usage['output_tokens']
            check(all(type(v) is int and v>=0 for v in (i,c,o)) and c<=i, 'PROVIDER_USAGE')
            expected = (((i-c)*Decimal(rates['input'])+c*Decimal(rates['cached'])+
                         o*Decimal(rates['output']))/1000000).quantize(Decimal('.00000001'), rounding=ROUND_CEILING)
            check(Decimal(provider['cost'])==expected, 'PROVIDER_RATE_RECALCULATION')
            check(i==response['input_tokens'] and o==response['output_tokens'], 'TOKEN_USAGE_MISMATCH')
            text = ''.join(c['text'] for item in env.get('output', []) if item.get('type')=='message'
                           for c in item.get('content', []) if c.get('type')=='output_text')
            if env.get('status') != 'completed': text = ''
            check(text.encode()==base64.b64decode(response['response_base64']), 'MODEL_RESPONSE_SUBSTITUTED')
        amount = float(response['actual_cost_usdt'])
        if amount:
            expected_charges[fid] = amount
            check(fid in cost_events and rid in proposals and at(cost_events[fid]['ready_at'])==at(proposals[rid]['at']), 'MODEL_COST_TIME_CHANGED')
        if rid in accepted:
            validated = accepted[rid]; decision = ai.get(fid)
            check(decision is not None and decision['context_hash']==digest(context), 'MODEL_CONTEXT_CHANGED')
            check(decision['plan']==validated['payload']['plan'] and
                  decision['plan']==json.loads(base64.b64decode(response['response_base64'])), 'MODEL_PLAN_CHANGED')
            check(at(decision['ready_at'])==at(validated['at']) and
                  decision.get('valid_until')==validated['payload']['valid_until'], 'MODEL_TIMING_CHANGED')
    check(charges==expected_charges, 'MODEL_COST_NOT_DEBITED')
    approx(paper['summary']['AI']['model_costs_paid']+paper['summary']['AI'].get('pending_model_cost_usdt',0), float(evidence['model_cost_usdt']['AI']), label='all_model_costs')
    termination = read(root/'termination.json')
    complete = (not termination['error'] and termination['collector_returncode']==0 and
                not evidence['unresolved_requests'] and not termination['model_uncertain']
                and paper['summary']['AI'].get('pending_model_cost_usdt',0)==0)
    return {'status': 'PASS_CONNECTION_ONLY' if complete and config['mode']=='FREE_PROBE_NOT_AI'
            else 'PASS_PRICED_PAPER_ONLY' if complete else 'INCOMPLETE',
            'mode': config['mode'], 'source_decision_audit': evidence,
            'paper_independent_audit': recalculation, 'summary': paper['summary'],
            'cost_method': 'MODEL_USAGE_COST_DEBITED_BEFORE_NEXT_FILL_NOT_PROVIDER_INVOICE',
            'economic_alpha': 'NOT_ESTABLISHED', 'real_orders': 0}


if __name__=='__main__':
    parser = argparse.ArgumentParser()
    parser.add_argument('--run-dir', type=Path, required=True)
    print(json.dumps(verify(parser.parse_args().run_dir), sort_keys=True))
