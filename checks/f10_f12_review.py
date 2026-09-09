import sys,json,copy,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import f11_allocate as fixtures
import f12_loop as loopfixtures
import paper_a as p,allocate_a as a,paper_loop as loop
class Review(unittest.TestCase):
 def setUp(self):
  self.c=fixtures.Allocation();self.c.setUp();self.addCleanup(self.c.doCleanups)
 def test_execution_preserves_ranked_quantity_despite_ticker(self):
  decisions=self.c.reserve();ident=decisions[0]['reservation_id']
  with patch.object(self.c.ex,'fetch_ticker',return_value={'last':200}):
   result=a.execute(self.c.s,self.c.path,self.c.ex,ident)
  self.assertEqual(p.dec(result['targets']['base']),p.dec(self.c.ranking['rows'][0]['base_quantity']))
 def test_crossed_funding_boundary_invalidates_rank(self):
  self.c.ranking['rows'][0]['next_funding_ms']=int(loop.time.time()*1000)-1
  self.assertEqual(self.c.reserve()[0]['reason'],'FUNDING_BOUNDARY_CROSSED')
 def test_execute_rechecks_funding_boundary(self):
  self.c.ranking['rows'][0]['next_funding_ms']=int(loop.time.time()*1000)+100
  ident=self.c.reserve()[0]['reservation_id']
  with patch('time.time',return_value=loop.time.time()+.2):
   with self.assertRaisesRegex(ValueError,'FUNDING_BOUNDARY_CROSSED'):a.execute(self.c.s,self.c.path,self.c.ex,ident)
  self.assertEqual(self.c.s['allocations'][ident]['status'],'RESERVED')
  self.assertFalse(self.c.s['positions'])
 def test_fresh_check_with_stale_book_blocked(self):
  now=loop.time.time()
  reports={'x':{'action':'HOLD','data_status':'COMPLETE','completed_at':now,'evidence_at':{'funding':now},'metrics':{'spot_execution':{'book_timestamp':int((now-6)*1000)}}}}
  self.assertFalse(loop.positions_ready(reports))
 def test_loop_rejects_expired_hold_check(self):
  c=loopfixtures.Loop();c.setUp()
  try:
   c.q.put(c.snapshot())
   old=loop.time.time()-20
   with patch.object(loop,'check_positions',return_value={'x':{'action':'HOLD','completed_at':old,'evidence_at':{'funding':old},'metrics':{}}}):
    result=c.tick()
   self.assertEqual(result['decision'],'POSITION_OR_FUNDING_REVIEW')
   self.assertFalse(json.loads(c.c.path.read_text()).get('allocations'))
  finally:c.doCleanups()
 def test_loop_batch_marks_all_reservations_before_first_execution(self):
  c=loopfixtures.Loop();c.setUp()
  try:
   c.q.put(c.snapshot());seen=[];original=a.execute
   def execute(state,path,ex,ident):
    disk=json.loads(path.read_text());seen.append(all(x.get('owner')=='paper_loop' for x in disk['allocations'].values() if x['status']=='RESERVED'))
    return original(state,path,ex,ident)
   with patch.object(a,'execute',side_effect=execute):c.tick()
   self.assertTrue(all(seen))
  finally:c.doCleanups()
if __name__=='__main__':unittest.main(verbosity=2)
