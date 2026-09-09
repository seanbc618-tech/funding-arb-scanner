import sys,json,queue,copy,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import paper_loop as loop
import f11_allocate as fixtures
import paper_a as p
class Loop(unittest.TestCase):
 def setUp(self):
  self.c=fixtures.Allocation();self.c.setUp();self.addCleanup(self.c.doCleanups);self.q=queue.Queue()
 def snapshot(self,gap=None):
  return {'state_identity':loop.identity(json.loads(self.c.path.read_text())),'started_at_ms':int(loop.time.time()*1000),'completed_at_ms':int(loop.time.time()*1000),'ranking':self.c.ranking,'funding':[],'gap':gap}
 def tick(self):return loop.tick(self.c.path,self.c.ex,self.q,self.c.limits)
 def test_empty_scanner_still_checks(self):
  with patch.object(loop,'check_positions',return_value={}) as check:
   self.assertEqual(self.tick()['decision'],'WAIT_SCAN');check.assert_called_once()
 def test_failed_scan_still_checks(self):
  self.q.put(self.snapshot('TimeoutError'))
  with patch.object(loop,'check_positions',return_value={}) as check:
   self.assertEqual(self.tick()['decision'],'SCAN_UNAVAILABLE');check.assert_called_once()
 def test_pipeline_two_coins(self):
  self.q.put(self.snapshot());r=self.tick();self.assertEqual(r['decision'],'PAPER_EXECUTED');self.assertEqual(len(r['executions']),2)
 def test_pending_blocks_before_scan(self):
  self.c.s['pending']={'reserved_usdt':'1'};p.save(self.c.path,self.c.s);self.q.put(self.snapshot());r=self.tick();self.assertEqual(r['reconciliation_gap'],'PENDING_EXECUTION');self.assertFalse(self.q.empty())
 def test_stale_snapshot_no_allocations(self):
  x=self.snapshot();x['completed_at_ms']-=6000;self.q.put(x);self.assertEqual(self.tick()['decision'],'STALE_SCAN')
 def test_risk_exit_never_automatically_closes(self):
  self.q.put(self.snapshot())
  with patch.object(loop,'check_positions',return_value={'x':{'action':'EXIT'}}):self.assertEqual(self.tick()['decision'],'POSITION_OR_FUNDING_REVIEW')
  self.assertEqual(json.loads(self.c.path.read_text())['trades'],{})
 def test_funding_gap_blocks_new(self):
  x=self.snapshot();x['funding']=[{'symbol':'BTC/USDT:USDT','gap':'TRUNCATED','cycles':[]}];self.q.put(x);self.assertEqual(self.tick()['decision'],'POSITION_OR_FUNDING_REVIEW')
 def test_partial_stops_remaining_and_releases(self):
  self.q.put(self.snapshot());self.c.ex.depth=.2;r=self.tick();self.assertEqual(r['decision'],'EXECUTION_REVIEW')
  s=json.loads(self.c.path.read_text());self.assertFalse(any(a['status']=='RESERVED' for a in s['allocations'].values()))
 def test_history_mismatch_blocks(self):
  self.c.s['positions']['BTC/USDT']={'quantity':'1'};p.save(self.c.path,self.c.s);r=self.tick();self.assertEqual(r['reconciliation_gap'],'POSITION_HISTORY_MISMATCH')
 def test_queue_drain_nonblocking_latest(self):
  self.q.put(1);self.q.put(2);self.assertEqual(loop.drain(self.q),2);self.assertIsNone(loop.drain(self.q))
 def test_state_changed_since_scan_blocks(self):
  x=self.snapshot();x['state_identity']='old';self.q.put(x);self.assertEqual(self.tick()['decision'],'STATE_CHANGED_RESCAN')
 def test_no_funding_positions_no_api(self):
  self.assertEqual(loop.collect_funding(self.c.ex,self.c.s),[])
 def test_cached_funding_settles_before_scan_error(self):
  import f09_funding as fixture
  case=fixture.Funding();case.setUp()
  try:
   self.c.path=case.path
   x=self.snapshot('SCAN_FAILED');x['funding']=[{'symbol':fixture.S,'gap':None,'cycles':[{'at':fixture.T,'rate':case.ex.rate,'price':case.ex.price}]}]
   case.s['trades']['x'].update(phase='open',coin='BTC',trade_id='x');p.save(case.path,case.s)
   x['state_identity']=loop.identity(case.s);self.q.put(x)
   with patch.object(loop,'check_positions',return_value={}):r=self.tick()
   self.assertEqual(r['funding'][0]['results'][0]['status'],'POSTED')
   self.assertEqual(r['decision'],'SCAN_UNAVAILABLE')
  finally:case.doCleanups()
 def test_collect_truncated_history_declared(self):
  self.c.s['trades']['x']={'orders':[{'executed_at_ms':1,'quote':{'kind':'perp','filled':'1','symbol':'BTC/USDT:USDT'}}]}
  with patch.object(self.c.ex,'publicGetPublicFundingRateHistory',return_value={'code':'0','data':[]}):
   self.assertEqual(loop.collect_funding(self.c.ex,self.c.s)[0]['gap'],'INVALID_DECLARED_GAP_HISTORY_TRUNCATED')
 def test_collect_legacy_gap(self):
  self.c.s['trades']['x']={'orders':[{'quote':{'kind':'perp','filled':'1','symbol':'BTC/USDT:USDT'}}]}
  self.assertEqual(loop.collect_funding(self.c.ex,self.c.s)[0]['gap'],'LEGACY_FILL_TIME_MISSING')
if __name__=='__main__':unittest.main(verbosity=2)
