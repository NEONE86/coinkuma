"""Frozen v3 causal features and spot plans; research labels live elsewhere."""
from __future__ import annotations

from collections import defaultdict
import hashlib
import statistics

from .cashflow_alpha import default_config as inherited_config, run as account_run
from .cashflow_alpha_v2 import ChallengerPanel
from .historical_data import ms
from .observer import DAY, json_text

STRATEGIES = ('demand', 'compression', 'price_control')
ERAS = {'development': (ms('2019-01-01'), ms('2023-01-01')),
        'validation': (ms('2023-01-01'), ms('2025-01-01')),
        'challenge': (ms('2025-01-01'), ms('2026-10-01'))}
END = ms('2026-10-01')


def era_of(day):
    return next((name for name, (a, z) in ERAS.items() if a <= day < z), None)


def mature_in_era(day):
    era = era_of(day)
    return era is not None and day + 181 * DAY < ERAS[era][1]


def config_for(strategy, costs=None, delay=2):
    if strategy not in STRATEGIES or delay not in (2, 3):
        raise ValueError('unregistered_strategy_or_delay')
    c = inherited_config()
    c.update(version='ONE-Repricing-v3', strategy=strategy, universe_limit=100000,
             max_hold_days=168, profit_take_R=6.666666666666667,
             execution_delay=delay, notes='Fixed v3; no adds, leverage, or live orders.')
    if costs:
        c.update(costs)
    return c


class AtlasPanel(ChallengerPanel):
    """All historical eligible markets. No labels or final listing status here."""
    def __init__(self, histories, config):
        super().__init__(histories, config)
        self.weekly_rows = {}
        self.episodes = {}
        self.raw_candidates = {}

    def features(self, symbol, day, cache=False):
        key = (symbol, day)
        if cache and key in self.weekly_rows:
            return self.weekly_rows[key]
        i = self.index.get(symbol, {}).get(day)
        bi = self.index.get('BTCUSDT', {}).get(day)
        if i is None or bi is None or bi < 239 or i-self.segments[symbol][i]+1 < 240:
            return None
        bs = self.h[symbol]
        btc = self.h['BTCUSDT']
        p = bs[i].close
        avg = statistics.fmean
        tr = [max(bs[j].high-bs[j].low, abs(bs[j].high-bs[j-1].close),
                  abs(bs[j].low-bs[j-1].close)) for j in range(i-69, i+1)]
        old_tr = avg(tr[:-14])
        old_volume = avg(b.quote_volume for b in bs[i-34:i-6])
        r28 = p/bs[i-28].close-1
        br28 = btc[bi].close/btc[bi-28].close-1
        above = btc[bi].close > avg(b.close for b in btc[bi-199:bi+1])
        r = dict(symbol=symbol, day=day, close=p,
                 max_input_close_ms=max(bs[i].close_ms, btc[bi].close_ms),
                 history_days=i-self.segments[symbol][i]+1,
                 liquidity=self.liquidity[symbol][i],
                 r7=p/bs[i-7].close-1, r28=r28,
                 relative28=r28-br28,
                 relative90=p/bs[i-90].close-btc[bi].close/btc[bi-90].close,
                 sma50=avg(b.close for b in bs[i-49:i+1]),
                 volume_ratio=avg(b.quote_volume for b in bs[i-6:i+1])/old_volume if old_volume else 0.,
                 tr_ratio=avg(tr[-14:])/old_tr if old_tr else 1.,
                 prior20_high=max(b.high for b in bs[i-20:i]),
                 trailing20_high=max(b.high for b in bs[i-19:i+1]),
                 btc_above=above,
                 regime='BULL' if above and br28>0 else 'BEAR' if not above and br28<0 else 'TRANSITION')
        r['liquidity_bin'] = sum(r['liquidity'] >= x for x in (3e6, 10e6, 30e6, 100e6))
        r['age_bin'] = sum(r['history_days'] >= x for x in (365, 730))
        r.update(flags(r))
        if cache:
            self.weekly_rows[key] = r
        return r

    def prepare(self):
        """Chronological first qualifying event, independent of holdings/outcomes."""
        self.episodes = {s: defaultdict(set) for s in STRATEGIES}
        self.raw_candidates = {s: defaultdict(set) for s in STRATEGIES}
        last = {}
        for b in self.h['BTCUSDT']:
            day = b.open_ms
            if not mature_in_era(day) or (day//DAY+3) % 7 != 6:
                continue
            for symbol in self.universe(day):
                r = self.features(symbol, day, cache=True)
                for strategy in STRATEGIES:
                    if not r[strategy]:
                        continue
                    self.raw_candidates[strategy][day].add(symbol)
                    key = (strategy, symbol, era_of(day))
                    if day-last.get(key, -10**18) < 182*DAY:
                        continue
                    self.episodes[strategy][day].add(symbol)
                    last[key] = day
        return self


def flags(r):
    price = r['btc_above'] and r['close'] > r['sma50'] and 0 <= r['r28'] <= .30 and r['r7'] <= .15
    return dict(price_control=price,
                demand=price and r['volume_ratio'] >= 1.5 and r['relative28'] > 0,
                compression=price and r['tr_ratio'] <= .70 and r['close'] > r['prior20_high'] and r['relative90'] > 0)


def make_plan(panel, book, day, c, prices):
    strategy = c['strategy']
    chosen = panel.episodes[strategy].get(day, set())
    eq = book.equity(prices)
    held, new, decisions = [], [], []
    for symbol in sorted(set(book.positions) | chosen):
        r = panel.features(symbol, day, cache=True)
        p = book.positions.get(symbol)
        d = dict(symbol=symbol, signal_day_ms=day, max_input_close_ms=day+DAY-1)
        if r:
            d['max_input_close_ms'] = r['max_input_close_ms']
        if p:
            if not r or (day+DAY-p.opened_ms)/DAY >= c['max_hold_days']:
                d.update(state='EXIT', reasons=['TIME_OR_HISTORY'])
            else:
                stop = p.stop
                if r['close'] >= 1.5*p.average_entry:
                    stop = max(stop, .70*r['trailing20_high'])
                held.append(dict(symbol=symbol, target_weight=min(c['max_position_weight'],
                                 p.qty*r['close']/eq), risk_fraction=.005, stop=stop,
                                 reference_price=r['close'], allow_add=False, stage='PROBE'))
                d.update(state='HOLD', reasons=['FIXED_STOP_OR_DELAYED_TRAIL'])
        elif r and symbol in chosen:
            rank = (r['relative28']+.02*min(r['volume_ratio'], 3) if strategy == 'demand' else
                    r['relative90']+.1*(1-r['tr_ratio']) if strategy == 'compression' else r['r28'])
            new.append((rank, symbol, dict(symbol=symbol, target_weight=min(.20, .005/.15),
                        risk_fraction=.005, stop=.85*r['close'], reference_price=r['close'],
                        allow_add=True, stage='PROBE')))
            d.update(state='CANDIDATE', reasons=[strategy])
        decisions.append(d)
    new.sort(key=lambda x: (-x[0], x[1]))
    targets = held+[v[2] for v in new[:max(0, c['max_positions']-len(held))]]
    selected = {t['symbol'] for t in targets}
    for d in decisions:
        if d.get('state') == 'CANDIDATE':
            d['state'] = 'ENTER' if d['symbol'] in selected else 'WAIT_CAPACITY'
    total = sum(t['target_weight'] for t in targets)
    if total > c['max_gross_weight']:
        for t in targets:
            t['target_weight'] *= c['max_gross_weight']/total
    due = day+c['execution_delay']*DAY
    plan = dict(observed_ms=day+DAY, signal_day_ms=day, execute_after_ms=due,
                expires_ms=due+DAY-1, position_context={s:p.opened_ms for s,p in book.positions.items()},
                targets=targets, decisions=decisions, universe=panel.universe(day),
                opportunity_count=len(chosen),
                raw_qualifying_count=len(panel.raw_candidates[strategy].get(day, set())),
                strategy=strategy, fundamental_revaluation_evidence='QUALITATIVE_ONLY',
                mode='EXPERIMENTAL_PAPER_PLAN')
    plan['id'] = hashlib.sha256(json_text(plan).encode()).hexdigest()
    return plan


def run(panel, config, start, end, liquidate_end=True):
    return account_run(panel, config, start, end, liquidate_end=liquidate_end,
                       planner=make_plan, signal_schedule=lambda day: (day//DAY+3) % 7 == 6)
