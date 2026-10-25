"""Two preregistered spot challengers. No fitting, live orders, or leverage."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import random
import statistics

from .cashflow_alpha import default_config as v1_config, run as account_run
from .observer import DAY, json_text
from .walkforward import HistoricalPanel

A = 'relative_strength_recovery'
B = 'established_trend_controlled_pullback'
STRATEGIES = (A, B)


def default_config(strategy):
    if strategy not in STRATEGIES:
        raise ValueError('unknown_challenger')
    c = v1_config()
    c.update(version='ONE-Cashflow-Alpha-v2', strategy=strategy,
             min_hold_days=7 if strategy == A else 0,
             max_hold_days=84 if strategy == A else 21)
    return c


class ChallengerPanel(HistoricalPanel):
    """Caches contain causal rows keyed by date, never future membership."""
    def __init__(self, histories, config):
        super().__init__(histories, config)
        self.market_cache = {}
        self.universe_cache = {}
        self.challenger_rows = {}
        self.signal_cache = {}

    def market_day(self, day):
        if day not in self.market_cache:
            self.market_cache[day] = {s: x for s in self.h if (x := self.bar(s, day)) is not None}
        return self.market_cache[day]

    def universe(self, day):
        if day not in self.universe_cache:
            pool = [s for s in self.h if self.eligible(s, day)
                    and self.liquidity[s][self.index[s][day]] >= self.config['min_quote_volume']]
            pool.sort(key=lambda s: (-self.liquidity[s][self.index[s][day]], s))
            self.universe_cache[day] = pool[:self.config['universe_limit']]
        return self.universe_cache[day]


def signal_row(panel, symbol, day, strategy):
    key = (strategy, symbol, day)
    if key in panel.challenger_rows:
        return panel.challenger_rows[key]
    i = panel.index.get(symbol, {}).get(day)
    bi = panel.index.get('BTCUSDT', {}).get(day)
    if i is None or bi is None or i-panel.segments[symbol][i]+1 < 240 or bi < 239:
        panel.challenger_rows[key] = None
        return None
    bs = panel.h[symbol][i-239:i+1]
    btc = panel.h['BTCUSDT'][bi-239:bi+1]
    cs = [b.close for b in bs]
    mean = statistics.fmean
    last = cs[-1]
    tr = [max(bs[j].high-bs[j].low, abs(bs[j].high-cs[j-1]),
              abs(bs[j].low-cs[j-1])) for j in range(200, 240)]
    atr = mean(tr[-14:])
    initial_stop = last-2.5*atr
    # Latest *completed* calendar week. Partial weeks/months add no entry filter.
    weekday = (day//DAY+3) % 7
    sunday_i = 239-((weekday+1) % 7)
    m = dict(last_close=last, previous_close=cs[-2], daily_open=bs[-1].open,
             sma20=mean(cs[-20:]), sma50=mean(cs[-50:]), sma200=mean(cs[-200:]),
             sma50_20days_ago=mean(cs[-70:-20]),
             btc_risk_on=btc[-1].close > mean(b.close for b in btc[-200:]),
             return_7d=last/cs[-8]-1, return_28d=last/cs[-29]-1,
             relative_return_28d=(last/cs[-29]-1)-(btc[-1].close/btc[-29].close-1),
             relative_return_90d=(last/cs[-91]-1)-(btc[-1].close/btc[-91].close-1),
             drawdown_180d=1-last/max(b.high for b in bs[-180:]),
             drawdown_60d=1-last/max(b.high for b in bs[-60:]),
             close_to_sma20=last/mean(cs[-20:]), atr14=atr,
             stop_distance=(last-initial_stop)/last,
             completed_week_up=cs[sunday_i] > cs[sunday_i-7],
             completed_week_close_ms=bs[sunday_i].close_ms)
    if strategy == A:
        # Prior 28 days exclude the latest seven: [-35:-7]. Each SMA is causal.
        sma = {j: mean(cs[j-49:j+1]) for j in range(204, 240)}
        m['prior28_below_sma50_count'] = sum(cs[j] < sma[j] for j in range(205, 233))
        m['last7_crossed_above_sma50'] = any(
            cs[j-1] < sma[j-1] and cs[j] > sma[j] for j in range(233, 240))
        m['tr_contraction'] = mean(tr[-10:])/mean(tr[:-10]) if mean(tr[:-10]) > 0 else None
    lookback = 20 if strategy == A else 10
    r = dict(symbol=symbol, signal_day_ms=day,
             max_input_close_ms=max(bs[-1].close_ms, btc[-1].close_ms),
             initial_stop=initial_stop,
             support_stop=min(b.low for b in bs[-lookback:])-.25*atr,
             score=m['relative_return_28d' if strategy == A else 'relative_return_90d'], metrics=m)
    panel.challenger_rows[key] = r
    return r


def entry_reasons(row, strategy):
    m = row['metrics']
    tests = [('BTC_RISK_OFF', m['btc_risk_on']),
             ('STOP_DISTANCE', .03 <= m['stop_distance'] <= .18)]
    if strategy == A:
        tests += [('BELOW_TREND_HISTORY', m['prior28_below_sma50_count'] >= 14),
                  ('NO_RECOVERY_CROSS', m['last7_crossed_above_sma50'] and m['last_close'] > m['sma50']),
                  ('DRAWDOWN_180D', .10 <= m['drawdown_180d'] <= .60),
                  ('RELATIVE_28D', m['relative_return_28d'] >= .03),
                  ('RETURN_7D', m['return_7d'] <= .12),
                  ('RETURN_28D', 0 <= m['return_28d'] <= .30),
                  ('TR_CONTRACTION', m['tr_contraction'] is not None and m['tr_contraction'] <= .90),
                  ('WEEK_NOT_UP', m['completed_week_up'])]
    elif strategy == B:
        tests += [('TREND_NOT_ESTABLISHED', m['sma50'] > m['sma200'] and m['sma50'] > m['sma50_20days_ago']),
                  ('DRAWDOWN_60D', .08 <= m['drawdown_60d'] <= .25),
                  ('SMA20_BAND', .95 <= m['close_to_sma20'] <= 1.03),
                  ('DAILY_RECOVERY', m['last_close'] > m['previous_close'] and m['last_close'] > m['daily_open']),
                  ('RETURN_7D', m['return_7d'] <= .08),
                  ('RETURN_28D', -.15 <= m['return_28d'] <= .25),
                  ('RELATIVE_90D', m['relative_return_90d'] > 0)]
    else:
        raise ValueError('unknown_challenger')
    return [reason for reason, passed in tests if not passed]


def _candidates(panel, day, strategy):
    key = (strategy, day)
    if key not in panel.signal_cache:
        rows = {s: r for s in panel.universe(day) if (r := signal_row(panel, s, day, strategy))}
        reasons = {s: entry_reasons(r, strategy) for s, r in rows.items()}
        candidates = [s for s in rows if not reasons[s]]
        panel.signal_cache[key] = rows, reasons, candidates
    return panel.signal_cache[key]


def make_plan(panel, book, day, config, prices, control_seed=None):
    strategy = config['strategy']
    if strategy == A and (day//DAY+3) % 7 != 6:
        raise ValueError('signals_require_completed_sunday')
    rows, reasons, candidates = _candidates(panel, day, strategy)
    chosen = set(candidates)
    if control_seed is not None:
        # Opportunity count fixed at each original signal date. Draw from the
        # historical top80 with the same BTC gate and initial-stop feasibility.
        eligible = sorted(s for s, r in rows.items() if r['metrics']['btc_risk_on']
                          and .03 <= r['metrics']['stop_distance'] <= .18)
        rng = random.Random(f'{strategy}:{control_seed}:{day}')
        chosen = set(rng.sample(eligible, min(len(candidates), len(eligible))))
        ranks = {s: rng.random() for s in chosen}
    else:
        ranks = {s: rows[s]['score'] for s in chosen}
    eq = book.equity(prices)
    held, new, decisions = [], [], []
    for s in sorted(set(rows) | set(book.positions)):
        r = rows.get(s) or signal_row(panel, s, day, strategy)
        p = book.positions.get(s)
        d = dict(symbol=s, signal_day_ms=day, max_input_close_ms=day+DAY-1,
                 state='REJECT', reasons=reasons.get(s, ['NOT_IN_UNIVERSE']))
        if r:
            d['max_input_close_ms'] = r['max_input_close_ms']
        if p:
            age = (day+DAY-p.opened_ms)/DAY
            if not r or not r['metrics']['btc_risk_on'] or age >= config['max_hold_days'] or (
                    age >= config['min_hold_days'] and r['metrics']['last_close'] < r['metrics']['sma50']):
                d.update(state='EXIT', reasons=['RISK_OFF_TREND_TIME_OR_HISTORY'])
            else:
                m = r['metrics']
                stop = max(p.stop, r['support_stop'])
                weight = min(config['max_position_weight'],
                             config['confirmed_risk']/max((m['last_close']-stop)/m['last_close'], .03),
                             p.qty*m['last_close']/eq)
                held.append(dict(symbol=s, target_weight=weight, risk_fraction=.005,
                                 stop=stop, reference_price=m['last_close'], allow_add=False, stage='PROBE'))
                d.update(state='HOLD', reasons=['DELAYED_TRAILING_STOP'], metrics=m)
        elif s in chosen and r:
            m = r['metrics']
            new.append((ranks[s], s, dict(symbol=s,
                        target_weight=min(config['max_position_weight'], .005/m['stop_distance']),
                        risk_fraction=.005, stop=r['initial_stop'], reference_price=m['last_close'],
                        allow_add=True, stage='PROBE')))
            d.update(state='CANDIDATE', reasons=['RANDOM_CONTROL' if control_seed is not None else strategy], metrics=m)
        decisions.append(d)
    new.sort(key=lambda x: (-x[0], x[1]))
    targets = held+[x[2] for x in new[:max(0, config['max_positions']-len(held))]]
    selected = {t['symbol'] for t in targets}
    for d in decisions:
        if d['state'] == 'CANDIDATE':
            d['state'] = 'ENTER' if d['symbol'] in selected else 'WAIT_CAPACITY'
    total = sum(t['target_weight'] for t in targets)
    if total > config['max_gross_weight']:
        for t in targets:
            t['target_weight'] *= config['max_gross_weight']/total
    if control_seed is not None:
        # Keep all actionable decisions and membership, but avoid duplicating
        # hundreds of millions of unused rejection-feature fields in controls.
        decisions = [{k:v for k,v in d.items() if k != 'metrics'}
                     for d in decisions if d['state'] != 'REJECT']
    due = day+2*DAY
    plan = dict(observed_ms=day+DAY, signal_day_ms=day, execute_after_ms=due,
                expires_ms=due+DAY-1, position_context={s: p.opened_ms for s, p in book.positions.items()},
                targets=targets, decisions=decisions, universe=panel.universe(day),
                strategy=strategy, control_seed=control_seed, opportunity_count=len(candidates),
                fundamental_revaluation_evidence='NOT_CONNECTED', mode='EXPERIMENTAL_PAPER_PLAN')
    plan['id'] = hashlib.sha256(json_text(plan).encode()).hexdigest()
    return plan


def run(panel, config, start, end, missing='zero', liquidate_end=True, control_seed=None):
    strategy = config['strategy']
    return account_run(panel, config, start, end, missing, liquidate_end,
                       planner=lambda p, b, d, c, prices: make_plan(p, b, d, c, prices, control_seed),
                       signal_schedule=lambda day: strategy == B or (day//DAY+3) % 7 == 6)
