"""R3 negative and positive regressions. All fixtures are synthetic."""
import copy
from decimal import Decimal
import unittest

import test_economic_research_v4_r2 as helpers
from test_economic_research_v4 import SOURCE
from one_trading.economic_research import evidence, events, review, theses, recovery, cli
from one_trading.economic_research.locations import locate
from one_trading.economic_research.store import digest


class R3Tests(unittest.TestCase):
    def setUp(self):
        self.h = helpers.R2Tests()
        self.h.setUp()
        self.addCleanup(self.h.doCleanups)
        self.t = self.h.t

    def test_pointer_structure_escape_and_array_indices(self):
        raw = b'{"values":["1","1"],"00":"2","a/b":{"~key":"3"}}'
        for pointer in ('/values/00', '/values/+0', '/values/-1', '/values/01', '/values/-', '/values/ 0',
                        '/values/0.0', '/values/2', '/a~2b', '/a~', 'values/0', None, 0):
            with self.subTest(pointer=pointer), self.assertRaises(ValueError):
                locate(raw, dict(json_pointer=pointer), True)
        self.assertEqual(locate(raw, dict(json_pointer='/00'), True), ('2', ['json_path', [['key', '00']]]))
        self.assertEqual(locate(raw, dict(json_pointer='/a~1b/~0key'), True)[0], '3')
        self.assertNotEqual(locate(raw, dict(json_pointer='/values/0'), True)[1], locate(raw, dict(json_pointer='/values/1'), True)[1])
        self.assertEqual(locate(b'"1"', dict(json_pointer=''), True), ('1', ['json_path', []]))
        for locator in (dict(quote='1'), dict(quote='1', json_pointer='/values/0')):
            with self.assertRaises(ValueError):
                locate(raw, locator, True)
        with self.assertRaisesRegex(ValueError, 'duplicate_json_key'):
            locate(b'{"x":1,"x":2}', dict(json_pointer='/x'), True)
        self.assertEqual(locate(b'<b>1</b><b>1</b>', dict(quote='1'), True)[1], ['text_spans', [[3,4],[11,12]]])
        with self.assertRaisesRegex(ValueError, 'partial_numeric_quote'):
            locate(b'<b>1.0</b>', dict(quote='1'), True)
        self.assertEqual(locate(b'<b> 123 </b>', dict(quote=' 123 '), True)[1],
                         locate(b'<b> 123 </b>', dict(quote='123'), True)[1])

    def test_same_leaf_aliases_rejected_distinct_leaves_sum(self):
        t = self.t
        sid = t.fetch(SOURCE, b'{"values":["1","1"]}')
        base = dict(t.obs, observation_id='a', value='1', evidence_ids=[sid], locator=dict(snapshot_id=sid,json_pointer='/values/0'))
        a = evidence.observe(t.store, base)
        for loc in (dict(quote='1'), dict(json_pointer='/values/00')):
            with self.assertRaises(ValueError):
                evidence.observe(t.store, dict(base, observation_id='bad', locator=dict(snapshot_id=sid, **loc)))
        b = evidence.observe(t.store, dict(base, observation_id='b', locator=dict(snapshot_id=sid,json_pointer='/values/1')))
        total = evidence.observe(t.store, dict(base, observation_id='sum', value='2', epistemic_type='DERIVED_VALUE',
            input_observation_ids=['a','b'], calculation='SUM', derivation_version='test'))
        self.assertEqual(len(evidence.validate_observation(t.store,total)['fact_ids']), 2)
        alias = evidence.observe(t.store, dict(a, observation_id='alias', value='1e0'))
        with self.assertRaisesRegex(ValueError, 'duplicate_economic_event'):
            event = events.register_event(t.store, dict(t.event,event_id='a-event',revision_id='a-event',observation_ids=['a']))
            events.register_event(t.store, dict(event,event_id='alias-event',revision_id='alias-event',observation_ids=['alias']))

    def test_legacy_locator_contamination_audit_score_correction(self):
        t = self.t
        t.payload['forecasts'][0]['resolution_rule']['threshold'] = '2'
        t.registered(); t.advance(days=181)
        first = evidence.observe(t.store, self.h.h.measurement('1'))
        # Preserved historical rows simulate old accepted input; this is not a
        # supported API bypass assertion. Cross-version API proof is separate.
        alias = dict(first, observation_id='old-quote', locator=dict(snapshot_id=first['evidence_ids'][0],quote='1'))
        t.store.put('observation', 'old-quote', alias)
        total = dict(first, observation_id='old-sum', value='2', epistemic_type='DERIVED_VALUE',
            input_observation_ids=['out','old-quote'], calculation='SUM', derivation_version='old')
        t.store.put('observation','old-sum',total)
        bad = dict(resolution_id='bad',forecast_id='IMPLEMENTATION',status='RESOLVED',explanation='old',
            observation_ids=['old-sum'],evidence_ids=first['evidence_ids'],supersedes_resolution_id=None,
            resolution_at=t.time(),outcome=1,measured_value='2')
        t.store.put('resolution','bad',bad)
        before = [dict(r) for r in t.store.db.execute('SELECT * FROM records')]
        check = review.audit(t.store)
        self.assertEqual(check['status'],'ISSUES')
        self.assertIn('bad',[r['record_id'] for r in check['semantics']['checks']['R3-01']['issues']])
        with self.assertRaisesRegex(ValueError,'numeric_json_requires_pointer'):
            review.score(t.store)
        corrected = self.h.h.resolve(first,key='corrected',parent='bad')
        result = review.score(t.store)
        self.assertEqual(corrected['outcome'],0)
        self.assertEqual(Decimal(result['scores'][0]['mean_loss']),Decimal('.64'))
        self.assertEqual([r['outcome'] for r in result['resolved_only_training_labels']],[0])
        self.assertEqual(before,[dict(r) for r in t.store.db.execute('SELECT * FROM records')][:len(before)])
        self.assertEqual(review.audit(t.store)['status'],'ISSUES')

    def test_valid_pointer_can_replace_invalid_legacy_quote_without_retiming(self):
        t = self.t
        sid=t.fetch(SOURCE,b'{"value":"1"}')
        old=dict(t.obs,observation_id='old',value='1',available_at=t.time(),evidence_ids=[sid],
                 locator=dict(snapshot_id=sid,quote='1'))
        t.store.put('observation','old',old)
        t.advance(seconds=1)
        new=evidence.observe(t.store,dict(old,observation_id='new',locator=dict(snapshot_id=sid,json_pointer='/value')))
        self.assertEqual(new['available_at'],old['available_at'])
        self.assertEqual(t.store.get('observation','old'),old)
        self.assertEqual(review.audit(t.store)['status'],'ISSUES')

    def test_repeated_html_token_provenance_cannot_be_added_twice(self):
        t=self.t
        sid=t.fetch(SOURCE,b'<b> 1 </b><b>1</b>')
        first=evidence.observe(t.store,dict(t.obs,observation_id='html',value='1',evidence_ids=[sid],
            locator=dict(snapshot_id=sid,quote=' 1 ')))
        alias=evidence.observe(t.store,dict(first,observation_id='html-alias',value='1.0',locator=dict(snapshot_id=sid,quote='1')))
        self.assertEqual(len(evidence.validate_observation(t.store,first)['fact_ids']),2)
        self.assertEqual(evidence.validate_observation(t.store,first)['fact_ids'],evidence.validate_observation(t.store,alias)['fact_ids'])
        with self.assertRaisesRegex(ValueError,'duplicate_derived_fact'):
            evidence.observe(t.store,dict(first,observation_id='bad-sum',value='2',epistemic_type='DERIVED_VALUE',
                input_observation_ids=['html','html-alias'],calculation='SUM',derivation_version='test'))

    def legacy(self, monotonic=False):
        t = self.t
        b = self.h.multiasset_bundle(self.h.other_asset())
        theses.register_thesis(t.store,b['thesis'])
        for f in b['forecasts']:
            f.pop('target_asset_id')
            if monotonic and f['target_type']=='IMPLEMENTATION':
                f['resolution_rule']['observation_mode']='EVER_IN_WINDOW'
            # Unit fixture only. The supplied e140232 producer verifies the
            # same scenario through historical supported registration APIs.
            t.store.put('forecast', f['forecast_id'], dict(f, registered_at=t.time(),mode='PROSPECTIVE'))
        return b

    def isolate(self):
        for f in self.t.store.all('forecast'):
            recovery.quarantine_question(self.t.store,dict(quarantine_id='q-'+f['question_id'],question_id=f['question_id'],review_reason='Review missing target'))

    def candidate(self,b):
        f = copy.deepcopy(b['forecasts'][-1])
        f.update(forecast_id='recovered',question_id='recovered',target_asset_id=self.t.aid)
        return f

    def payload(self,f):
        return dict(recovery_id='link',quarantine_id='q-PRICE',review_reason='Explicit analyst target A; old target remains unknown',forecast=f)

    def test_quarantine_and_recovery_cli_preserve_history_report_counts(self):
        t = self.t; b = self.legacy(); f = self.candidate(b)
        before = [dict(r) for r in t.store.db.execute('SELECT * FROM records')]
        with self.assertRaisesRegex(ValueError,'unambiguous_target'):
            theses.register_forecast(t.store,f)
        t.advance(seconds=1); prior = b['forecasts'][0]['knowledge_cutoff']; self.isolate()
        with self.assertRaisesRegex(ValueError,'explicit_recovery_link_required'):
            theses.register_forecast(t.store,f)
        result = cli.ingest(t.store,[dict(kind='question_recovery',data=self.payload(f))])
        cli.ingest(t.store,[dict(kind='question_recovery',data=self.payload(f))]) # idempotent
        unrelated=copy.deepcopy(f); unrelated.update(forecast_id='other-question',question_id='other-question')
        unrelated['resolution_rule']['measurement']='unrelated'
        cli.ingest(t.store,[dict(kind='forecast',data=unrelated)])
        self.assertEqual(before,[dict(r) for r in t.store.db.execute('SELECT * FROM records')][:len(before)])
        scored = review.score(t.store)
        self.assertEqual(scored['counts']['registered_forecasts'],5)
        self.assertEqual(scored['counts']['quarantined_forecasts'],3)
        self.assertEqual(scored['counts']['selected_forecasts'],2)
        self.assertEqual(scored['resolved_only_training_labels'],[])
        self.assertEqual(len(scored['legacy_review']['recovery_links']),1)
        self.assertEqual(review.audit(t.store)['status'],'ISSUES')
        with self.assertRaisesRegex(ValueError,'unambiguous_target'):
            review.score(t.store,prior) # no future quarantine applied to old as_of
        with self.assertRaisesRegex(ValueError,'quarantined_question'):
            review.latest_resolution(t.store,'PRICE',t.time())
        with self.assertRaisesRegex(ValueError,'duplicate_forecast_question'):
            duplicate=copy.deepcopy(unrelated);duplicate.update(question_id='dup',forecast_id='dup')
            theses.register_forecast(t.store,duplicate)

    def test_recovery_invalid_changes_atomic_and_valid_history_cannot_hide(self):
        t = self.t; b=self.legacy();self.isolate();f=self.candidate(b)
        for changes in ({'target_asset_id':'bad'}, {'question_id':'PRICE'}, {'supersedes_forecast_id':'PRICE'}):
            with self.subTest(changes=changes), self.assertRaises(ValueError):
                recovery.recover_question(t.store,self.payload(dict(f,**changes)))
        changed=copy.deepcopy(f);changed['resolution_rule']['measurement']='wash-old-loss'
        with self.assertRaisesRegex(ValueError,'preserve_original_endpoint'):
            recovery.recover_question(t.store,self.payload(changed))
        bad=copy.deepcopy(f);bad['probability_or_distribution']['points'][0]['p']='.5'
        with self.assertRaisesRegex(ValueError,'probability_sum'):
            recovery.recover_question(t.store,self.payload(bad))
        self.assertEqual(t.store.all('question_recovery'),[])
        recovery.recover_question(t.store,self.payload(f))
        with self.assertRaisesRegex(ValueError,'only_legacy_missing_target'):
            recovery.quarantine_question(t.store,dict(quarantine_id='hide',question_id='recovered',review_reason='hide loss'))

    def test_known_legacy_outcome_cannot_be_recovered_as_prospective(self):
        t=self.t;b=self.legacy();self.isolate();f=self.candidate(b)
        t.store.put('resolution','known',dict(resolution_id='known',forecast_id='PRICE',status='RESOLVED',resolution_at=t.time(),outcome='50'))
        with self.assertRaisesRegex(ValueError,'cannot_reset_known'):
            recovery.recover_question(t.store,self.payload(f))
        self.assertEqual(t.store.all('question_recovery'),[])

    def test_expired_question_blocks_recovery_and_link(self):
        t=self.t;b=self.legacy();self.isolate();f=self.candidate(b)
        f=copy.deepcopy(b['forecasts'][0]);f.update(forecast_id='new',question_id='new',target_asset_id=t.aid)
        # Expired questions cannot be laundered by choosing a new identity.
        t.advance(days=181)
        payload=dict(self.payload(f),quarantine_id='q-IMPLEMENTATION')
        with self.assertRaisesRegex(ValueError,'prospective_outcome_already_known_or_due'):
            recovery.recover_question(t.store,payload)
        self.assertEqual(t.store.all('question_recovery'),[])

    def test_known_raw_measurement_blocks_recovery_and_rolls_back_link(self):
        t=self.t;b=self.legacy(monotonic=True);self.isolate()
        f=copy.deepcopy(b['forecasts'][0]);f.update(forecast_id='new',question_id='new',target_asset_id=t.aid)
        t.advance(seconds=1)
        measured=self.h.h.measurement('100')
        measured.update(period_start=t.time(),period_end=t.time())
        evidence.observe(t.store,measured)
        with self.assertRaisesRegex(ValueError,'prospective_outcome_already_known_or_due'):
            recovery.recover_question(t.store,dict(self.payload(f),quarantine_id='q-IMPLEMENTATION'))
        self.assertEqual(t.store.all('question_recovery'),[])


if __name__ == '__main__':
    unittest.main()
