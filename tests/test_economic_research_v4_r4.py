"""Complete HTML numeric tokens and preserved legacy contamination; synthetic only."""
from decimal import Decimal
import unittest

import test_economic_research_v4 as helpers
from one_trading.economic_research import evidence, review
from one_trading.economic_research.locations import locate


class NumericTokenTests(unittest.TestCase):
    def test_complete_numeric_spellings_and_utf8_positions(self):
        for token in ('1e1', '1E1', '1e+1', '1e-1', '-1e1', '+1e1', '.5', '-.5',
                      '1.', '1.00', '1,000.50', '1,000e-1', '0', '-0.0', '+123'):
            with self.subTest(token=token):
                prefix = '매출<b>'
                raw = (prefix + token + '</b>원').encode('utf-8')
                start = len(prefix.encode('utf-8'))
                self.assertEqual(locate(raw, dict(quote=token), True),
                                 (token.replace(',', ''), ['text_spans', [[start, start + len(token)]]]))

    def test_subtokens_and_unsupported_spellings_cannot_be_measurements(self):
        for full, fragment in (
            ('1e1', '1'), ('1E1', '1'), ('1e+1', '+1'), ('1e-1', '-1'),
            ('-1e1', '1e1'), ('+1e1', '1e1'), ('1.00', '1'), ('1.00', '00'),
            ('.5', '5'), ('-1', '1'), ('1,000.50', '1'), ('1,000.50', '000'),
            ('1,000.50', '50'), ('1_000', '000'), ('１2', '2'), ('1２', '1'),
            ('−1', '1'), ('1−2', '1'), ('1−2', '2'), ('1+2', '2'),
            ('1e1e2', '2'), ('1e', '1'), ('12,34', '12'), ('12,34', '34'),
        ):
            with self.subTest(full=full, fragment=fragment), self.assertRaisesRegex(ValueError, 'partial_numeric_quote'):
                locate(('<b>' + full + '</b>').encode('utf-8'), dict(quote=fragment), True)
        for token in ('1_000', '12,34', '1e1e2', '１２', '1e', '1+2'):
            with self.subTest(token=token), self.assertRaisesRegex(ValueError, 'invalid_numeric_quote'):
                locate(('<b>' + token + '</b>').encode('utf-8'), dict(quote=token), True)
        with self.assertRaisesRegex(ValueError, 'numeric_quote_requires_utf8'):
            locate(b'\xff<b>1</b>', dict(quote='1'), True)

    def test_standalone_spans_exclude_exponent_suffix_and_preserve_text(self):
        raw = b'<b>1e1</b><b> 1 </b><b>1</b>'
        self.assertEqual(locate(raw, dict(quote='1'), True)[1], ['text_spans', [[14, 15], [23, 24]]])
        self.assertEqual(locate(raw, dict(quote=' 1 '), True)[1], locate(raw, dict(quote='1'), True)[1])
        # TEXT quotations retain literal substring behavior; only numeric facts
        # require a complete numeric token.
        self.assertEqual(locate(b'<b>1e1</b>', dict(quote='1'), False)[1],
                         ['text_spans', [[3, 4], [5, 6]]])


class NumericOutcomeTests(unittest.TestCase):
    def setUp(self):
        self.h = helpers.V4Tests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.h.payload['forecasts'][0]['resolution_rule']['threshold'] = '11'
        self.h.registered()
        self.h.advance(days=181)

    def observation(self, key, sid, value, quote):
        h = self.h
        r = h.store.get('forecast', 'IMPLEMENTATION')['resolution_rule']
        return dict(observation_id=key, asset_id=h.aid, metric=r['measurement'], value=value,
                    value_type='DECIMAL', unit=r['unit'], period_start=r['window_start'],
                    period_end=r['window_end'], epistemic_type='DIRECT_RECORD', evidence_ids=[sid],
                    locator=dict(snapshot_id=sid, quote=quote), derivation_version=None)

    def resolve(self, observation, key='result', parent=None):
        return review.resolve_forecast(self.h.store, dict(resolution_id=key, forecast_id='IMPLEMENTATION',
            status='RESOLVED', explanation='Synthetic number-token regression',
            observation_ids=[observation['observation_id']], evidence_ids=observation['evidence_ids'],
            supersedes_resolution_id=parent))

    def test_exponent_suffix_cannot_flip_outcome_or_training_label(self):
        h = self.h
        sid = h.fetch(helpers.SOURCE, b'<b>1e1</b>')
        full = evidence.observe(h.store, self.observation('full', sid, '10', '1e1'))
        with self.assertRaisesRegex(ValueError, 'partial_numeric_quote'):
            evidence.observe(h.store, self.observation('suffix', sid, '1', '1'))
        self.assertIsNone(h.store.get('observation', 'suffix', optional=True))
        self.assertEqual(self.resolve(full)['outcome'], 0)
        score = review.score(h.store)
        self.assertEqual(Decimal(score['scores'][0]['mean_loss']), Decimal('.64'))
        self.assertEqual([r['outcome'] for r in score['resolved_only_training_labels']], [0])
        self.assertEqual(review.audit(h.store)['status'], 'PASS')

    def test_genuinely_separate_html_numbers_still_sum(self):
        h = self.h
        sid = h.fetch(helpers.SOURCE, b'<b>1e1</b><b>1</b>')
        full = evidence.observe(h.store, self.observation('full', sid, '10', '1e1'))
        evidence.observe(h.store, self.observation('one', sid, '1', '1'))
        total = evidence.observe(h.store, dict(full, observation_id='sum', value='11',
            epistemic_type='DERIVED_VALUE', input_observation_ids=['full', 'one'],
            calculation='SUM', derivation_version='r4-regression'))
        self.assertEqual(self.resolve(total)['outcome'], 1)
        self.assertEqual(review.audit(h.store)['status'], 'PASS')

    def test_legacy_bad_label_is_blocked_until_append_only_correction(self):
        h = self.h
        sid = h.fetch(helpers.SOURCE, b'<b>1e1</b>')
        full = evidence.observe(h.store, self.observation('full', sid, '10', '1e1'))
        # These preserved historical-format rows were accepted by v4.3. Direct
        # fixture injection is explicit; the separate cross-version verifier
        # creates them through v4.3's supported APIs.
        suffix = dict(self.observation('old-suffix', sid, '1', '1'), available_at=h.time())
        h.store.put('observation', 'old-suffix', suffix)
        old_sum = dict(full, observation_id='old-sum', value='11', epistemic_type='DERIVED_VALUE',
                       input_observation_ids=['full', 'old-suffix'], calculation='SUM', derivation_version='old')
        h.store.put('observation', 'old-sum', old_sum)
        old_result = dict(resolution_id='old-result', forecast_id='IMPLEMENTATION', status='RESOLVED',
            explanation='Previously accepted synthetic result', observation_ids=['old-sum'], evidence_ids=[sid],
            supersedes_resolution_id=None, resolution_at=h.time(), outcome=1, measured_value='11')
        h.store.put('resolution', 'old-result', old_result)
        before = [dict(r) for r in h.store.db.execute('SELECT * FROM records ORDER BY sequence')]
        audit = review.audit(h.store)
        self.assertEqual(audit['status'], 'ISSUES')
        self.assertIn('old-result', [r['record_id'] for r in audit['semantics']['checks']['R3-01']['issues']])
        with self.assertRaisesRegex(ValueError, 'partial_numeric_quote'):
            review.score(h.store)
        self.assertEqual(self.resolve(full, 'corrected', 'old-result')['outcome'], 0)
        score = review.score(h.store)
        self.assertEqual(Decimal(score['scores'][0]['mean_loss']), Decimal('.64'))
        self.assertEqual([r['outcome'] for r in score['resolved_only_training_labels']], [0])
        self.assertEqual(before, [dict(r) for r in h.store.db.execute('SELECT * FROM records ORDER BY sequence')][:len(before)])
        self.assertEqual(review.audit(h.store)['status'], 'ISSUES')


if __name__ == '__main__':
    unittest.main()
