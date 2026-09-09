import sys,copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import allocate_a as a,paper_a as p,rank_a as r
from f10_rank import Exchange,N
class Allocation(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'paper.json'
  self.s=p.initial(1000,'.001','.0005');p.save(self.path,self.s)
  self.ex=Exchange();self.clock=patch('time.time',return_value=N/1000);self.clock.start();self.addCleanup(self.clock.stop)
  for sym,m in list(self.ex.markets.items()):self.ex.markets[sym.replace('BTC','ETH')]=copy.deepcopy(m)
  row=r.evaluate(self.ex,'BTC',100,720,1,0,50);row['rank']=1
  eth=copy.deepcopy(row);eth['coin']='ETH';eth['rank']=2
  eth['fees']={s.replace('BTC','ETH'):v for s,v in eth['fees'].items()}
  for q in eth['quotes']:q['symbol']=q['symbol'].replace('BTC','ETH')
  self.ranking={'rows':[row,eth]};self.limits={'per_coin_gross_usdt':250,'total_gross_usdt':500,'max_positions':2,'buffer_usdt':100}
 def reserve(self):return a.reserve_batch(self.s,self.path,self.ranking,self.limits)
 def test_two_reservations_no_double_spend(self):
  out=self.reserve();self.assertEqual([x['status'] for x in out],['RESERVED']*2);self.assertLess(p.available(self.s),600)
 def test_capital_shortage(self):
  self.s['cash_usdt']='450';out=self.reserve();self.assertEqual(out[1]['reason'],'INSUFFICIENT_UNRESERVED_CAPITAL')
 def test_coin_limit(self):
  self.limits['per_coin_gross_usdt']=150;self.assertEqual(self.reserve()[0]['reason'],'PER_COIN_GROSS_LIMIT')
 def test_total_limit(self):
  self.limits['total_gross_usdt']=300;self.assertEqual(self.reserve()[1]['reason'],'TOTAL_GROSS_LIMIT')
 def test_position_limit(self):
  self.limits['max_positions']=1;self.assertEqual(self.reserve()[1]['reason'],'MAX_POSITIONS')
 def test_duplicate_reserve(self):
  self.reserve();before=p.available(self.s);self.assertEqual(self.reserve()[0]['reason'],'COIN_ALREADY_ALLOCATED');self.assertEqual(p.available(self.s),before)
 def test_release_restores_budget(self):
  out=self.reserve();a.release(self.s,self.path,out[0]['reservation_id']);a.release(self.s,self.path,out[1]['reservation_id']);self.assertEqual(p.available(self.s),1000)
 def test_two_execute_and_close(self):
  out=self.reserve()
  for x in out:a.execute(self.s,self.path,self.ex,x['reservation_id'])
  self.assertEqual(len([t for t in self.s['trades'].values() if t['phase']=='open']),2)
  self.assertEqual(a.reserved(self.s),0)
  for coin in ['BTC','ETH']:p.operate(self.s,self.path,self.ex,coin,closing=True)
  self.assertTrue(all(t['phase']=='closed' for t in self.s['trades'].values()))
 def test_partial_fill_release_and_keep_exposure(self):
  out=self.reserve();self.ex.depth=.2;a.execute(self.s,self.path,self.ex,out[0]['reservation_id'])
  self.assertEqual(self.s['allocations'][out[0]['reservation_id']]['status'],'FINISHED')
  self.assertEqual(next(iter(self.s['trades'].values()))['phase'],'needs_close')
  with self.assertRaisesRegex(ValueError,'UNFINISHED'):a.execute(self.s,self.path,self.ex,out[1]['reservation_id'])
 def test_no_direct_open_bypass(self):
  self.reserve()
  with self.assertRaisesRegex(ValueError,'ALLOCATION_REQUIRED'):p.operate(self.s,self.path,self.ex,'BTC',100)
 def test_price_jump_blocks_before_fill(self):
  out=self.reserve();orig=self.ex.fetch_order_book
  def book(*args,**kwargs):
   b=orig(*args,**kwargs)
   for side in ['asks','bids']:
    for level in b[side]:level[0]*=2
   return b
  with patch.object(self.ex,'fetch_order_book',side_effect=book):
   with self.assertRaises(ValueError):a.execute(self.s,self.path,self.ex,out[0]['reservation_id'])
  self.assertFalse(self.s['positions'])
 def test_expired_preserves_reservation(self):
  out=self.reserve()
  with patch('time.time',return_value=N/1000+10):
   with self.assertRaisesRegex(ValueError,'EXPIRED'):a.execute(self.s,self.path,self.ex,out[0]['reservation_id'])
  self.assertEqual(self.s['allocations'][out[0]['reservation_id']]['status'],'RESERVED')
 def test_fee_mismatch(self):
  self.s['fees']['spot']='.002';self.assertEqual(self.reserve()[0]['reason'],'PAPER_AND_RANKING_FEES_DIFFER')
 def test_partial_cash_matches_actual_and_remaining_reservation(self):
  out=self.reserve();self.ex.depth=.2;a.execute(self.s,self.path,self.ex,out[0]['reservation_id'])
  held=sum((p.dec(x['margin_usdt']) for x in self.s['positions'].values()),p.dec(0))
  remaining=p.dec(self.s['allocations'][out[1]['reservation_id']]['capital_usdt'])
  self.assertEqual(p.available(self.s),p.dec(self.s['cash_usdt'])-held-remaining)
  self.assertEqual(a.reserved(self.s),remaining)
 def test_uncertain_fill_preserves_pending_and_blocks_next(self):
  out=self.reserve()
  with patch.object(p,'apply_fill',side_effect=OSError('fill failure')):
   with self.assertRaises(OSError):a.execute(self.s,self.path,self.ex,out[0]['reservation_id'])
  self.assertIsNotNone(self.s['pending'])
  with self.assertRaisesRegex(ValueError,'UNFINISHED'):a.execute(self.s,self.path,self.ex,out[1]['reservation_id'])
 def test_orphan_executing_never_auto_replayed(self):
  out=self.reserve();self.s['allocations'][out[0]['reservation_id']]['status']='EXECUTING';p.save(self.path,self.s)
  with self.assertRaisesRegex(ValueError,'UNFINISHED'):a.execute(self.s,self.path,self.ex,out[1]['reservation_id'])
  with self.assertRaisesRegex(ValueError,'RELEASABLE'):a.release(self.s,self.path,out[0]['reservation_id'])
 def test_atomic_reserve_failure_no_mutation(self):
  before=copy.deepcopy(self.s)
  with patch.object(a,'save',side_effect=OSError):
   with self.assertRaises(OSError):self.reserve()
  self.assertEqual(before,self.s)
if __name__=='__main__':unittest.main(verbosity=2)
