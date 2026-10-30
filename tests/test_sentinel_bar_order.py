"""Regression for the independently reported historical-bar replacement."""
import hashlib,json,tempfile,unittest
from pathlib import Path
from scripts.replay_bar_order import run,bar,stamp,T,SYMBOL
from one_sentinel.engine import Observer

def put(o,source,at,value,**kw):return o.ingest(source,json.dumps(value,sort_keys=True).encode(),at,at,timing=stamp(at),**kw)

class BarOrder(unittest.TestCase):
    def test_delayed_old_bar_preserves_control_decision_and_latest_kline(self):
        with tempfile.TemporaryDirectory() as tmp:
            a,control,_=run(Path(tmp)/'control','control')
            b,delayed,_=run(Path(tmp)/'delayed','delayed')
            try:
                self.assertEqual((control['action'],control['reason']),('ENTER','ACCESS_HYPOTHESIS_ELIGIBLE'))
                self.assertEqual((delayed['action'],delayed['reason']),(control['action'],control['reason']))
                self.assertEqual(delayed['latest_bar'],control['latest_bar'])
                self.assertEqual(delayed['pending_outcomes'][0]['reason'],'OUT_OF_ORDER_BAR_TIME')
                self.assertEqual(delayed['release_count'],0)
            finally:a.close();b.close()
    def test_ordinary_out_of_order_bar_preserves_control_decision(self):
        with tempfile.TemporaryDirectory() as tmp:
            o,result,_=run(Path(tmp)/'ordinary','ordinary')
            try:
                self.assertEqual((result['action'],result['reason']),('ENTER','ACCESS_HYPOTHESIS_ELIGIBLE'))
                self.assertEqual(result['latest_bar']['close'],T+59.999)
                self.assertEqual(result['quarantines'][-1]['error'],'OUT_OF_ORDER_BAR_TIME')
            finally:o.close()
    def test_other_symbol_older_bar_is_independent(self):
        with tempfile.TemporaryDirectory() as tmp:
            o,result,_=run(Path(tmp)/'other','other_symbol')
            try:
                self.assertEqual(result['action'],'ENTER');self.assertEqual(result['release_count'],1)
                self.assertEqual(o.l.latest_input('bar','BTCUSDT')['parsed']['close'],T-.001)
                self.assertEqual(o.l.latest_input('bar',SYMBOL)['parsed']['close'],T+59.999)
            finally:o.close()
    def test_newer_pending_and_newer_normal_bar_advance(self):
        with tempfile.TemporaryDirectory() as tmp:
            o,_,_=run(Path(tmp)/'r','control')
            try:
                r=put(o,'bar',T+120.1,bar(T+120.09,T+60,'77777'))
                self.assertEqual(o.l.get('interpretation',r['id'])['items'][0]['state'],'UNCERTAIN')
                o.tick(T+120.5,stamp(T+120.5))
                self.assertEqual(o.l.last('clock_pending_result')['reason'],'SAFE_PAST_AND_FRESH')
                self.assertEqual(o.l.latest_input('bar',SYMBOL)['parsed']['close'],T+119.999)
                put(o,'clock',T+180.6,dict(serverTime=round((T+180.595)*1000)),context=dict(request_timing=stamp(T+180.59)))
                r=put(o,'bar',T+180.8,bar(T+180.7,T+120,'88888'))
                self.assertEqual(o.l.latest_input('bar',SYMBOL)['id'],r['id'])
                self.assertEqual(o.l.latest_input('bar',SYMBOL)['parsed']['close'],T+179.999)
            finally:o.close()
    def test_late_old_uncertain_interpretation_cannot_replace_current_eligibility(self):
        with tempfile.TemporaryDirectory() as tmp:
            o,result,_=run(Path(tmp)/'r','control')
            try:
                r=put(o,'bar',T+61.2,bar(T+61.19,T-60,'1000'))
                self.assertEqual(o.l.get('interpretation',r['id'])['items'][0]['state'],'UNCERTAIN')
                self.assertEqual(o.l.item_state('bar',SYMBOL)['state'],'VALID')
                o.tick(T+62,stamp(T+62));self.assertEqual(o.l.last('clock_pending_result')['reason'],'OUT_OF_ORDER_BAR_TIME')
                self.assertEqual(o.l.latest_input('bar',SYMBOL)['id'],result['latest_bar_input_id'])
            finally:o.close()
    def test_duplicate_conflict_and_out_of_order_preserve_original_bytes(self):
        for volume,reason in [('99000','DUPLICATE_BAR'),('1000','CONFLICTING_BAR_REVISION')]:
            for pending in (False,True):
                with self.subTest(reason=reason,pending=pending),tempfile.TemporaryDirectory() as tmp:
                    o,result,_=run(Path(tmp)/'r','control')
                    try:
                        r=put(o,'bar',T+61.2,bar(T+(61.19 if pending else 60.8),T,volume))
                        interpretation=o.l.get('interpretation',r['id']);raw=o.l.root/'raw'/(r['sha256']+'.bin')
                        o.tick(T+62,stamp(T+62))
                        if pending:self.assertEqual(o.l.last('clock_pending_result')['reason'],reason)
                        elif reason=='DUPLICATE_BAR':self.assertEqual(o.l.get('dedup',r['id'])['original'],result['latest_bar_input_id'])
                        else:self.assertEqual(o.l.get('quarantine',r['id'])['error'],reason)
                        self.assertEqual(o.l.get('interpretation',r['id']),interpretation)
                        self.assertEqual(o.l.get('receipt',r['id']),r);self.assertEqual(hashlib.sha256(raw.read_bytes()).hexdigest(),r['sha256'])
                        self.assertEqual(o.l.latest_input('bar',SYMBOL)['id'],result['latest_bar_input_id'])
                        self.assertEqual(o.l.item_state('bar',SYMBOL)['state'],'VALID')
                    finally:o.close()
    def test_projection_defends_old_record_replay_and_rebuild(self):
        with tempfile.TemporaryDirectory() as tmp:
            root=Path(tmp)/'r';o,_,_=run(root,'control')
            try:
                first=o.l.latest_input('bar',SYMBOL)
                put(o,'clock',T+120.6,dict(serverTime=round((T+120.595)*1000)),context=dict(request_timing=stamp(T+120.59)))
                newest=put(o,'bar',T+120.8,bar(T+120.7,T+60,'77777'))
                seq=o.l.db.execute("SELECT seq FROM records WHERE kind='input' AND id=?",(first['id'],)).fetchone()[0]
                with o.l.transaction():o.l._project(seq,'input',first)
                self.assertEqual(o.l.latest_input('bar',SYMBOL)['id'],newest['id'])
                with o.l.transaction():
                    for table in ('input_index','bar_index','item_index','clock_pending_index','projection_cursor'):o.l.db.execute('DELETE FROM '+table)
                o.close();o=Observer(root)
                self.assertEqual(o.l.latest_input('bar',SYMBOL)['id'],newest['id'])
                self.assertEqual(o.l.latest_input('bar')['id'],newest['id'])
                self.assertEqual(o.l.item_state('bar',SYMBOL)['state'],'VALID')
            finally:o.close()
if __name__=='__main__':unittest.main()
