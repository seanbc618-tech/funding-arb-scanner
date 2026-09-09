import sys,copy,json,queue,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import paper_a as p,paper_loop as loop,exit_a
import f11_allocate as fixtures
class Exit(unittest.TestCase):
 def setUp(self):
  self.c=fixtures.Allocation();self.c.setUp();self.addCleanup(self.c.doCleanups)
  d=self.c.reserve()[0];import allocate_a
  self.t=allocate_a.execute(self.c.s,self.c.path,self.c.ex,d['reservation_id'])
  now=loop.time.time();self.report={'action':'HOLD','reasons':[],'data_status':'COMPLETE','gaps':[], 'completed_at':now,'evidence_at':{'funding':now},'metrics':{},'execution':{'status':'READY'},'risk':{'scope':'local_and_public'}}
 def run_exit(self):return exit_a.run(self.c.s,self.c.path,self.c.ex,{self.t['trade_id']:self.report})
 def test_positive_hold(self):self.assertEqual(self.run_exit()[0]['action'],'HOLD')
 def test_negative_closes(self):
  self.report['reasons']=['FUNDING_NEGATIVE'];self.assertEqual(self.run_exit()[0]['result'],'CLOSED');self.assertTrue(all(p.dec(x['quantity'])==0 for x in self.c.s['positions'].values()))
 def test_risk_priority_above_funding(self):
  self.report['reasons']=['FUNDING_NEGATIVE','EXPOSURE_LIMIT'];self.assertEqual(exit_a.decide(self.t,self.report)['priority'],0)
 def test_explicit_hold_limit(self):
  t=copy.deepcopy(self.t);t['opened_at']-=7200
  self.assertEqual(exit_a.decide(t,self.report,1)['reason'],'MAX_HOLD_REACHED')
  self.assertEqual(exit_a.decide(t,self.report)['action'],'HOLD')
 def test_missing_data_even_with_risk_blocks(self):
  self.report.update(reasons=['EXPOSURE_LIMIT'],gaps=['book missing']);self.assertEqual(self.run_exit()[0]['action'],'BLOCK')
 def test_stale_blocks(self):
  self.report['completed_at']-=6;self.assertEqual(self.run_exit()[0]['reason'],'STALE_CHECK')
 def test_pending_no_trade(self):
  self.c.s['pending']={'reserved_usdt':'10'};before=copy.deepcopy(self.c.s);self.assertEqual(self.run_exit()[0]['reason'],'UNKNOWN_EXECUTION');self.assertEqual(before,self.c.s)
 def test_partial_exit_continues_confirmed(self):
  self.report['reasons']=['FUNDING_NEGATIVE'];self.c.ex.depth=1
  self.assertEqual(self.run_exit()[0]['result'],'PARTIAL_REQUIRES_NEXT_CHECK')
  loop.reconcile(self.c.s,allow_exit_continuation=True)
  with self.assertRaises(ValueError):loop.reconcile(self.c.s)
  self.c.ex.depth=100;self.report['reasons']=['INCOMPLETE_EXECUTION'];self.assertEqual(self.run_exit()[0]['result'],'CLOSED')
 def test_exit_before_scan_even_empty(self):
  self.report['reasons']=['FUNDING_NEGATIVE']
  with patch.object(loop,'check_positions',return_value={self.t['trade_id']:self.report}):
   r=loop.tick(self.c.path,self.c.ex,queue.Queue(),self.c.limits,auto_exit=True)
  self.assertEqual(r['decision'],'EXIT_PROCESSED');self.assertNotIn('allocations',r)
 def test_default_switch_no_exit(self):
  self.report['reasons']=['FUNDING_NEGATIVE']
  with patch.object(loop,'check_positions',return_value={self.t['trade_id']:self.report}):
   loop.tick(self.c.path,self.c.ex,queue.Queue(),self.c.limits)
  self.assertEqual(json.loads(self.c.path.read_text())['trades'][self.t['trade_id']]['phase'],'open')
 def test_exit_request_durable_before_close(self):
  self.report['reasons']=['FUNDING_NEGATIVE'];original=p.operate
  def close(*args,**kwargs):
   self.assertIn('exit_request',json.loads(self.c.path.read_text())['trades'][self.t['trade_id']]);return original(*args,**kwargs)
  with patch.object(p,'operate',side_effect=close):self.run_exit()
 def test_confirmed_zero_remainder_finalized_after_interrupted_phase_write(self):
  self.report['reasons']=['FUNDING_NEGATIVE'];self.run_exit()
  t=self.c.s['trades'][self.t['trade_id']];t['phase']='closing';p.save(self.c.path,self.c.s)
  incomplete={'action':'BLOCK','data_status':'INCOMPLETE','gaps':['NO_POSITION']}
  with patch.object(loop,'check_positions',return_value={self.t['trade_id']:incomplete}):
   loop.tick(self.c.path,self.c.ex,queue.Queue(),self.c.limits,auto_exit=True)
  self.assertEqual(json.loads(self.c.path.read_text())['trades'][self.t['trade_id']]['phase'],'closed')
 def test_mixed_close_success_and_failure_report_review(self):
  decisions=[{'action':'EXIT','result':'CLOSED'},{'action':'EXIT','result':'FAILED_REQUIRES_CHECK'}]
  with patch.object(loop,'check_positions',return_value={}),patch.object(exit_a,'run',return_value=decisions):
   r=loop.tick(self.c.path,self.c.ex,queue.Queue(),self.c.limits,auto_exit=True)
  self.assertEqual(r['decision'],'EXIT_REVIEW')
 def test_flat_pending_not_finalized(self):
  self.report['reasons']=['FUNDING_NEGATIVE'];self.run_exit()
  t=self.c.s['trades'][self.t['trade_id']];t['phase']='closing';self.c.s['pending']={'reserved_usdt':'1'};p.save(self.c.path,self.c.s)
  self.assertEqual(self.run_exit()[0]['reason'],'UNKNOWN_EXECUTION');self.assertEqual(t['phase'],'closing')
 def test_flat_missing_fill_history_not_finalized(self):
  self.report['reasons']=['FUNDING_NEGATIVE'];self.run_exit()
  t=self.c.s['trades'][self.t['trade_id']];t['phase']='closing';t['orders']=[]
  self.assertFalse(exit_a.confirmed_flat(self.c.s,t))
 def test_real_account_risk_not_used_for_paper(self):
  self.report['risk']['scope']='account_and_local';self.assertEqual(self.run_exit()[0]['reason'],'UNSUPPORTED_PAPER_RISK_SCOPE')
if __name__=='__main__':unittest.main(verbosity=2)
