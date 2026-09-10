import sys,copy,json,queue,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import paper_a as p,allocate_a,rank_a,rotate_a as r,recovery_a as recovery,paper_loop as loop
import f11_allocate as fixtures

class RotationRecovery(unittest.TestCase):
 def setUp(self):
  self.c=fixtures.Allocation();self.c.setUp();self.addCleanup(self.c.doCleanups)
  d=self.c.reserve()[0];self.t=allocate_a.execute(self.c.s,self.c.path,self.c.ex,d['reservation_id'])
  for ident,a in list(self.c.s['allocations'].items()):
   if a['status']=='RESERVED':allocate_a.release(self.c.s,self.c.path,ident)
  self.settings=dict(min_hold_hours=0,cooldown_hours=24,buffer_usdt=0,confirm_seconds=1,confirmations=2,max_daily=1)
  self.old=copy.deepcopy(self.c.ranking['rows'][0]);self.row=copy.deepcopy(self.c.ranking['rows'][1])
  # 用实际 quote/evaluate 重算小于旧仓释放预算的新仓，不伪造费用或资金。
  self.row=rank_a.evaluate(self.c.ex,'ETH',99,720,1,0,50);self.row['rank']=1
  self.row['expected_funding_usdt']='30';self.row['expected_net_usdt']=str(p.dec(30)-p.dec(self.row['roundtrip_cost_usdt']))
 def run_rotate(self):
  return r.run(self.c.s,self.c.path,self.c.ex,{'rows':[self.row]},{self.t['trade_id']:self.old},self.c.limits,self.settings,int(loop.time.time()*1000))
 def advance(self,seconds=1):
  loop.time.time.return_value+=seconds
  for row in (self.row,self.old):
   row['computed_at_ms']=int(loop.time.time()*1000)
   for q in row['quotes']:q['book_timestamp']=row['computed_at_ms']
 def test_29_day_arithmetic(self):
  self.assertEqual(p.dec(4)/(p.dec(1000)*p.dec('.05')/365),p.dec('29.2'))
 def test_incremental_not_sunk_entry(self):
  e=r.economics(self.c.s,self.t,self.old,self.row,0)
  self.assertTrue(e['eligible']);self.assertEqual(e['incremental_cost_usdt'],self.row['roundtrip_cost_usdt'])
  old=copy.deepcopy(self.old);old['fee_cost_usdt']='999'
  self.assertEqual(r.economics(self.c.s,self.t,old,self.row,0),e)
 def test_insufficient_gain_or_buffer(self):
  self.assertFalse(r.economics(self.c.s,self.t,self.old,self.row,100)['eligible'])
  self.row['expected_funding_usdt']='0';self.assertFalse(r.economics(self.c.s,self.t,self.old,self.row,0)['eligible'])
 def test_budget_no_topup(self):
  big=copy.deepcopy(self.c.ranking['rows'][1])
  with self.assertRaisesRegex(ValueError,'CAPITAL'):r.economics(self.c.s,self.t,self.old,big,0)
 def test_window_mismatch(self):
  self.row['hold_hours']='24'
  with self.assertRaisesRegex(ValueError,'WINDOW'):r.economics(self.c.s,self.t,self.old,self.row,0)
 def test_confirmations_independent_fresh_scans(self):
  self.assertEqual(self.run_rotate()['confirmation']['count'],1)
  self.assertEqual(self.run_rotate()['confirmation']['count'],1)
  self.advance();self.assertEqual(self.run_rotate()['reason'],'SWITCH_EXIT_REQUESTED')
  self.assertEqual(self.run_rotate()['reason'],'OLD_POSITION_NOT_FLAT')
  self.assertNotIn('ETH/USDT',self.c.s['positions'])
 def test_confirmation_gap_resets(self):
  self.run_rotate();self.advance(20);self.assertEqual(self.run_rotate()['confirmation']['count'],1)
 def begin_and_close(self):
  self.run_rotate();self.advance();self.run_rotate()
  p.operate(self.c.s,self.c.path,self.c.ex,'BTC',closing=True)
  self.assertEqual(self.run_rotate()['reason'],'REQUOTE_AFTER_CONFIRMED_FLAT');self.advance()
 def test_opportunity_disappears_cash(self):
  self.begin_and_close();self.row['status']='REJECT_NET'
  self.assertEqual(self.run_rotate()['reason'],'OPPORTUNITY_DISAPPEARED')
  self.assertEqual(self.run_rotate()['reason'],'SWITCH_CASH_COOLDOWN')
 def test_complete_rotation_and_daily_cap(self):
  self.begin_and_close()

  for ident,a in list(self.c.s['allocations'].items()):
   if a['status']=='RESERVED':allocate_a.release(self.c.s,self.c.path,ident)
  self.assertEqual(self.run_rotate()['reason'],'SWITCH_COMPLETED')
  self.assertEqual(self.c.s['rotations'][0]['phase'],'DONE');self.assertTrue(any(t['coin']=='ETH' and t['phase']=='open' for t in self.c.s['trades'].values()))
  self.assertEqual(self.run_rotate()['action'],'NONE')
 def test_positive_requote_loses_incremental_advantage_cash(self):
  self.begin_and_close();self.row['expected_net_usdt']='1'
  self.assertEqual(self.run_rotate()['reason'],'REQUOTE_NOT_ELIGIBLE')
 def test_manual_reserved_target_not_selected(self):
  allocate_a.reserve_batch(self.c.s,self.c.path,{'rows':[self.row]},self.c.limits)
  self.assertEqual(self.run_rotate()['action'],'NONE');self.assertFalse(self.c.s['rotations'])
 def test_audit_cash_and_duplicate_evidence(self):
  recovery.audit(self.c.s)
  bad=copy.deepcopy(self.c.s);bad['cash_usdt']='1000'
  with self.assertRaisesRegex(ValueError,'CASH'):recovery.audit(bad)
  bad=copy.deepcopy(self.c.s);bad['trades'][self.t['trade_id']]['orders']*=2
  with self.assertRaisesRegex(ValueError,'IDENTITY'):recovery.audit(bad)
 def test_pending_unapplied_abandoned_never_filled(self):
  before=copy.deepcopy(self.c.s['positions']);cash=self.c.s['cash_usdt']
  self.c.s['pending']={'order_id':'lost','trade_id':self.t['trade_id'],'reserved_usdt':'1','quote':copy.deepcopy(self.t['orders'][0]['quote'])};p.save(self.c.path,self.c.s)
  result=recovery.run(self.c.s,self.c.path)
  self.assertEqual(result['status'],'RECOVERED');self.assertIsNone(self.c.s['pending'])
  self.assertEqual(self.c.s['positions'],before);self.assertEqual(self.c.s['cash_usdt'],cash)
  self.assertEqual(self.c.s['trades'][self.t['trade_id']]['exit_request']['reason'],'RECOVERY_KNOWN_REMAINDER')
 def test_pending_committed_conflict_blocks_no_write(self):
  self.c.s['pending']=copy.deepcopy(self.t['orders'][0]);p.save(self.c.path,self.c.s);before=self.c.path.read_bytes()
  self.assertEqual(recovery.run(self.c.s,self.c.path)['gap'],'PENDING_COMMITTED_ATOMICITY_CONFLICT');self.assertEqual(before,self.c.path.read_bytes())
 def test_orphan_allocation_released_not_executed(self):
  ds=allocate_a.reserve_batch(self.c.s,self.c.path,{'rows':[self.row]},self.c.limits)
  a=self.c.s['allocations'][ds[0]['reservation_id']];a['status']='EXECUTING';p.save(self.c.path,self.c.s)
  before=len(self.c.s['trades']);recovery.run(self.c.s,self.c.path)
  self.assertEqual(self.c.s['allocations'][a['id']]['status'],'FAILED');self.assertEqual(len(self.c.s['trades']),before)
 def test_phase_interruption_known_flat(self):
  p.operate(self.c.s,self.c.path,self.c.ex,'BTC',closing=True)
  self.c.s['trades'][self.t['trade_id']]['phase']='closing';p.save(self.c.path,self.c.s)
  self.assertEqual(recovery.run(self.c.s,self.c.path)['status'],'RECOVERED')
  self.assertEqual(self.c.s['trades'][self.t['trade_id']]['phase'],'closed');recovery.audit(self.c.s)
 def test_atomic_recovery_save_failure_preserves_memory(self):
  self.c.s['trades'][self.t['trade_id']]['phase']='needs_close';before=copy.deepcopy(self.c.s)
  with patch.object(recovery,'save',side_effect=OSError):
   with self.assertRaises(OSError):recovery.run(self.c.s,self.c.path)
  self.assertEqual(before,self.c.s)
 def test_partial_open_then_actual_remainder_close(self):
  p.operate(self.c.s,self.c.path,self.c.ex,'BTC',closing=True)
  recovery.run(self.c.s,self.c.path)
  ds=allocate_a.reserve_batch(self.c.s,self.c.path,{'rows':[self.old]},self.c.limits,owner='paper_loop');self.c.ex.depth=.2
  t=allocate_a.execute(self.c.s,self.c.path,self.c.ex,ds[0]['reservation_id']);self.assertEqual(t['phase'],'needs_close')
  recovery.run(self.c.s,self.c.path);self.c.ex.depth=100
  # Real check_a public risk path, rather than a mocked HOLD result.
  orig=self.c.ex.fetch_funding_rate
  self.c.ex.fetch_funding_rate=lambda s:orig(s)|{'fundingRate':.001,'fundingTimestamp':int(loop.time.time()*1000)+3600000}
  result=loop.tick(self.c.path,self.c.ex,queue.Queue(),self.c.limits,auto_exit=True,auto_recover=True)
  self.assertEqual(result['decision'],'EXIT_PROCESSED');recovery.audit(json.loads(self.c.path.read_text()))
 def test_bad_state_blocks_all_exchange_calls(self):
  self.c.s['cash_usdt']='42';p.save(self.c.path,self.c.s)
  with patch.object(loop,'check_positions') as check:
   result=loop.tick(self.c.path,self.c.ex,queue.Queue(),self.c.limits,auto_exit=True,auto_recover=True)
  check.assert_not_called();self.assertEqual(result['recovery']['status'],'BLOCK')
 def test_every_open_durable_boundary_restart(self):
  class Crash(BaseException):pass
  for boundary in range(1,7):
   with self.subTest(boundary=boundary):
    state=p.initial(1000,'.001','.0005');p.save(self.c.path,state)
    decisions=allocate_a.reserve_batch(state,self.c.path,{'rows':[self.old]},self.c.limits,owner='paper_loop')
    count=[0];original=p.save
    def crash_after(path,value):
     original(path,value);count[0]+=1
     if count[0]==boundary:raise Crash()
    with patch.object(p,'save',side_effect=crash_after):
     with self.assertRaises(Crash):allocate_a.execute(state,self.c.path,self.c.ex,decisions[0]['reservation_id'])
    state=json.loads(self.c.path.read_text());result=recovery.run(state,self.c.path)
    self.assertNotEqual(result['status'],'BLOCK',result)
    recovery.audit(state)
    for t in list(state['trades'].values()):
     if t['phase'] not in ('closed','open'):p.operate(state,self.c.path,self.c.ex,t['coin'],closing=True)
    recovery.audit(state)
    self.assertFalse(state['pending']);self.assertTrue(all(t['phase'] in ('closed','open') for t in state['trades'].values()))
 def test_every_close_durable_boundary_restart(self):
  class Crash(BaseException):pass
  for boundary in range(1,7):
   with self.subTest(boundary=boundary):
    state=copy.deepcopy(self.c.s);p.save(self.c.path,state)
    count=[0];original=p.save
    def crash_after(path,value):
     original(path,value);count[0]+=1
     if count[0]==boundary:raise Crash()
    with patch.object(p,'save',side_effect=crash_after):
     with self.assertRaises(Crash):p.operate(state,self.c.path,self.c.ex,'BTC',closing=True)
    state=json.loads(self.c.path.read_text());result=recovery.run(state,self.c.path)
    self.assertNotEqual(result['status'],'BLOCK',result)
    if state['trades'][self.t['trade_id']]['phase']!='closed':p.operate(state,self.c.path,self.c.ex,'BTC',closing=True)
    recovery.audit(state);self.assertTrue(all(p.dec(v['quantity'])==0 for v in state['positions'].values()))
 def test_rotation_phase_write_interruptions_no_duplicate_open(self):
  class Crash(BaseException):pass
  self.begin_and_close();baseline=copy.deepcopy(self.c.s)
  for boundary in (1,2,3):
   with self.subTest(boundary=boundary):
    self.c.s=copy.deepcopy(baseline);p.save(self.c.path,self.c.s);original=r.store;count=[0]
    def crash_after(*args):
     original(*args);count[0]+=1
     if count[0]==boundary:raise Crash()
    with patch.object(r,'store',side_effect=crash_after):
     with self.assertRaises(Crash):self.run_rotate()
    self.c.s=json.loads(self.c.path.read_text());self.assertNotEqual(recovery.run(self.c.s,self.c.path)['status'],'BLOCK')
    self.run_rotate();recovery.audit(self.c.s)
    self.assertLessEqual(len([t for t in self.c.s['trades'].values() if t['coin']=='ETH']),1)
    self.assertIn(self.c.s['rotations'][-1]['phase'],('DONE','CASH'))
if __name__=='__main__':unittest.main(verbosity=2)
