"""Closed-input model transport. No tools, retries, orders or implicit approval."""
import json
import os
import urllib.request
from datetime import datetime, timezone
from decimal import Decimal, ROUND_CEILING

from .engine import canonical, ReplayError, instant
from .planners import MODEL_INSTRUCTION, MODEL_SCHEMA, plan


def usage_cost(usage, rates):
    counts = [usage['input_tokens'], usage['input_tokens_details']['cached_tokens'],
              usage['output_tokens']]
    if any(type(n) is not int or n < 0 for n in counts) or counts[1] > counts[0]:
        raise ReplayError('INVALID_PROVIDER_USAGE')
    a, c, b = counts
    result = ((a-c)*Decimal(rates['input']) + c*Decimal(rates['cached']) +
              b*Decimal(rates['output'])) / Decimal(1000000)
    return str(result.quantize(Decimal('.00000001'), rounding=ROUND_CEILING))


class FreeProbe:
    """Deterministic wiring probe, explicitly NOT an AI strategy."""
    model = 'free-probe-not-ai'
    reserve = '0'
    def respond(self, context):
        if context['positions']:
            answer = plan('SELL', sorted(context['positions'])[0], 1.,
                          'Wiring probe exit; no alpha hypothesis')
        elif not context['pending_order']:
            answer = plan('BUY', 'BTCUSDT', .0011,
                          'Wiring probe entry; no alpha hypothesis')
        else:
            answer = plan('HOLD', 'CASH', 0., 'Wiring probe pending')
        return canonical(answer), {'kind': 'FREE_PROBE_NOT_AI', 'cost': '0',
                                   'input_tokens': 0, 'output_tokens': 0}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *args, **kwargs):
        raise ReplayError('MODEL_REDIRECT_FORBIDDEN')


class ResponsesModel:
    """Pricing-pinned transport; uncertain completion stops further model calls.

    Rates must be independently approved USD/million token rates, including a
    USD:USDT accounting assumption. No silent CLI subscription-cost assumption.
    Input reservation uses a conservative byte-token bound plus protocol margin;
    an overrun is a failed cost audit, never a zero-cost response.
    """
    def __init__(self, approval):
        if approval.get('model_calls_authorized') is not True:
            raise ReplayError('MODEL_APPROVAL_REQUIRED')
        for name in ('input', 'cached', 'output'):
            value = Decimal(approval['rates'][name])
            if not value.is_finite() or value < 0:
                raise ReplayError('BAD_APPROVED_RATE')
        if not approval.get('approval_reference') or approval.get('usd_usdt') != '1':
            raise ReplayError('APPROVAL_AND_FX_ASSUMPTION_REQUIRED')
        if (type(approval.get('max_output_tokens')) is not int or
                not 16 <= approval['max_output_tokens'] <= 4096):
            raise ReplayError('OUTPUT_TOKEN_BOUND')
        reserve = Decimal(approval['reserve_per_call_usdt'])
        if not reserve.is_finite() or reserve <= 0:
            raise ReplayError('RESERVATION_REQUIRED')
        self.approval = approval
        self.model = approval['model']
        self.reserve = approval['reserve_per_call_usdt']
        self.key = os.environ.get('OPENAI_API_KEY')
        if not self.key:
            raise ReplayError('MODEL_CREDENTIAL_UNAVAILABLE')

    def respond(self, context):
        if instant(self.approval['expires_at']) <= datetime.now(timezone.utc):
            raise ReplayError('APPROVAL_EXPIRED_BEFORE_DISPATCH')
        body = dict(model=self.model, instructions=MODEL_INSTRUCTION,
                    input=canonical(context).decode(), tools=[], store=False,
                    max_output_tokens=self.approval['max_output_tokens'],
                    text={'format': {'type': 'json_schema', 'name': 'paper_plan',
                                     'strict': True, 'schema': MODEL_SCHEMA}})
        wire = canonical(body)
        bound = ((len(wire)+4096)*Decimal(self.approval['rates']['input']) +
                 self.approval['max_output_tokens']*Decimal(self.approval['rates']['output']))/1000000
        if bound > Decimal(self.reserve):
            raise ReplayError('REQUEST_EXCEEDS_RESERVED_COST')
        request = urllib.request.Request('https://api.openai.com/v1/responses', data=wire,
            headers={'Authorization': 'Bearer '+self.key, 'Content-Type': 'application/json'})
        with urllib.request.build_opener(NoRedirect).open(request, timeout=30) as response:
            raw = response.read(1000001)
            if len(raw) > 1000000:
                raise ReplayError('MODEL_RESPONSE_TOO_LARGE')
        envelope = json.loads(raw)
        cost = usage_cost(envelope['usage'], self.approval['rates'])
        text = ''.join(c['text'] for item in envelope.get('output', [])
                       if item.get('type') == 'message' for c in item.get('content', [])
                       if c.get('type') == 'output_text')
        if envelope.get('status') != 'completed':
            text = ''  # Charge and preserve incomplete/refused output, no plan.
        # Preserve the full provider response, even a refusal/incomplete result.
        return text.encode(), {'kind': 'OPENAI_USAGE_PRICED_NOT_INVOICE', 'cost': cost,
            'input_tokens': envelope['usage']['input_tokens'],
            'output_tokens': envelope['usage']['output_tokens'],
            'provider_response': envelope, 'provider_raw_utf8': raw.decode(),
            'request_body': body}
