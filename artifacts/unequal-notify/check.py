import copy,json,sys,tempfile,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import unequal_notify as n
class Check(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.d=Path(self.tmp.name);self.cursor=self.d/'cursor.json';self.sent=[]
  self.now=1789458000
  self.s={'started_at_ms':self.now*1000-3600000,'coverage_gaps':[],'groups':{'1':{'events':[]}}}
  self.r={'at_ms':self.now*1000,'status':'OBSERVING','rows':[],'coverage_gaps':[]};self.write()
 def write(self):
  (self.d/'state.json').write_text(json.dumps(self.s));(self.d/'report.json').write_text(json.dumps(self.r))
 def send(self,msg):self.sent.append(msg);return {'status':'CONFIRMED','message_id':7}
 def tick(self):return n.tick(self.d,self.cursor,self.send,self.now)
 def test_initial_then_no_duplicate_then_hour(self):
  self.assertEqual(self.tick()['status'],'CONFIRMED');self.assertEqual(self.tick()['status'],'IDLE');self.assertEqual(len(self.sent),1)
  self.now+=3600;self.tick();self.assertEqual(len(self.sent),2)
 def test_old_events_baseline_and_new_funding(self):
  self.s['groups']['1']['events']=[{'type':'FUNDING','payment_usdt':'.1'}];self.write();self.tick();self.assertNotIn('新增',self.sent[-1])
  self.s['groups']['1']['events'].append({'type':'FUNDING','payment_usdt':'.2'});self.write();self.assertEqual(self.tick()['new_events'],1)
  self.assertEqual(self.tick()['status'],'IDLE')
 def test_unknown_never_retried(self):
  self.assertEqual(n.tick(self.d,self.cursor,lambda _: {'status':'DELIVERY_UNKNOWN'},self.now)['status'],'DELIVERY_UNKNOWN')
  self.assertEqual(self.tick()['status'],'IDLE')
 def test_stale_hides_valuation_and_gap_disclosed(self):
  self.r['at_ms']-=6000;self.r['coverage_gaps']=[{'gap':'BOOK_TIME_GAP'}];self.write();self.tick()
  self.assertIn('估值未知',self.sent[-1]);self.assertIn('正式比较无效',self.sent[-1])
 def test_cursor_rejects_new_experiment(self):
  self.tick();self.s['started_at_ms']+=1;self.write()
  with self.assertRaisesRegex(ValueError,'MISMATCH'):self.tick()
if __name__=='__main__':unittest.main(verbosity=2)
