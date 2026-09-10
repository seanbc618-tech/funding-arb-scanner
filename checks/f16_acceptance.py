import sys,copy,json,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import ccxt
import paper_a as p,paper_acceptance as a,paper_replay as replay
import f10_rank as fixtures


def dataset():
 n=fixtures.N;iv=fixtures.IV;last=1788912000000
 ex=fixtures.Exchange();markets={}
 for coin in ('BTC','ETH'):
  for suffix in ('',':USDT'):
   sym=f'{coin}/USDT{suffix}';m=copy.deepcopy(ex.markets['BTC/USDT'+suffix])
   m.update(symbol=sym,id=coin+'-USDT'+('-SWAP' if suffix else ''),base=coin,precision={'amount':.001,'price':.01})
   markets[sym]=m
 frames=[]
 times=[n+i*1000 for i in range(8)]+[last+iv+61000,last+iv+62000]
 for i,now in enumerate(times):
  books={};fees={};funding={};history={};candles={}
  for coin in ('BTC','ETH'):
   for suffix in ('',':USDT'):
    sym=f'{coin}/USDT{suffix}'
    with patch('time.time',return_value=now/1000):books[sym]=ex.fetch_order_book(sym)
    fees[sym]={'symbol':sym,'taker':'.0005' if suffix else '.001'}
   inst=coin+'-USDT-SWAP';sym=f'{coin}/USDT:USDT';last_at=now//iv*iv;next_at=last_at+iv
   rate=('.003' if i==0 else '.0001') if coin=='BTC' else ('.0001' if i==0 else '.002')
   if i==len(times)-1:rate='-.001'
   funding[sym]={'fundingRate':rate,'fundingTimestamp':next_at,'info':{'instId':inst,'method':'current_period','fundingTime':str(next_at),'nextFundingTime':str(next_at+iv),'fundingRate':rate}}
   history[inst]=[{'instId':inst,'fundingTime':str(last_at-j*iv),'realizedRate':'.003' if coin=='BTC' else '.002'} for j in range(24)]
   candles[inst]=[[str(last_at),'100','101','99','100','1']] if now-last_at>=60000 else []
  frames.append(dict(at_ms=now,books=books,fees=fees,funding=funding,history=history,mark_candles=candles))
 return {'source':'SYNTHETIC_SCENARIO','ccxt_version':ccxt.__version__,'precision_mode':ccxt.TICK_SIZE,'markets':markets,'coins':['BTC','ETH'],
         'cash_usdt':1000,'spot_fee':'.001','perp_fee':'.0005','scenario':dict(notional=100,hold_hours=720,margin_ratio=1,reserve_usdt=100,basis_stress_bps=50),
         'limits':dict(per_coin_gross_usdt=250,total_gross_usdt=250,max_positions=1,buffer_usdt=100),
         'switch_policy':dict(min_hold_hours=0,cooldown_hours=24,buffer_usdt=0,confirm_seconds=1,confirmations=2,max_daily=1),'frames':frames}

class Acceptance(unittest.TestCase):
 def setUp(self):
  self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup);self.path=Path(self.tmp.name)/'evidence';self.state=p.initial(1000,'.001','.0005');self.config={'mode':'test'}
  self.clock=patch('time.time',return_value=fixtures.N/1000);self.clock.start();self.addCleanup(self.clock.stop)
 def begin(self,hours=72):return a.begin(self.path,self.state,hours,60,self.config)
 def report(self):
  now=int(a.time.time()*1000)
  return dict(scanner_alive=True,decision='HOLD_CASH',last_scan_success_ms=now,last_position_check_ms=now,runtime_config=self.config)
 def test_short_run_not_72h_acceptance(self):
  self.begin();a.record(self.path,self.state,self.report());self.assertEqual(a.assess(self.path)['status'],'OBSERVING')
 def test_elapsed_without_events_not_pass(self):
  self.begin('.001');a.record(self.path,self.state,self.report());a.time.time.return_value+=4;a.record(self.path,self.state,self.report())
  self.assertEqual(a.assess(self.path)['status'],'INCOMPLETE_EVENTS')
 def test_observation_gap_invalid(self):
  self.begin();a.time.time.return_value+=61;a.record(self.path,self.state,self.report());self.assertEqual(a.assess(self.path)['status'],'INVALID_DECLARED_GAP')
 def test_baseline_tamper_detected(self):
  self.begin();(self.path/'baseline.json').write_text('{}')
  with self.assertRaisesRegex(ValueError,'HASH'):a.assess(self.path)
 def test_state_tamper_detected(self):
  self.begin();key=a.record(self.path,self.state,self.report());(self.path/f'state-{key}.json').write_text('{}')
  with self.assertRaisesRegex(ValueError,'HASH'):a.assess(self.path)
 def test_config_change_refused(self):
  self.begin();r=self.report();r['runtime_config']={}
  with self.assertRaisesRegex(ValueError,'CONFIG'):a.record(self.path,self.state,r)
 def test_code_change_refused(self):
  self.begin()
  with patch.object(a,'code_digest',return_value='different'):
   with self.assertRaisesRegex(ValueError,'CODE'):a.record(self.path,self.state,self.report())
 def test_scanner_running_but_stale_rejected(self):
  self.begin();r=self.report();r['last_scan_success_ms']-=61000;a.record(self.path,self.state,r)
  self.assertEqual(a.assess(self.path)['status'],'INVALID_DECLARED_GAP')
 def test_full_replay_cycle_and_actual_costs(self):
  result=replay.replay(dataset(),self.path)
  self.assertEqual(result['status'],'PASS_SYNTHETIC_REPLAY',result['gaps'])
  self.assertEqual(result['metrics']['switch_completed'],1)
  self.assertTrue(result['metrics']['flat']);self.assertGreater(p.dec(result['metrics']['funding_usdt']),0)
  self.assertEqual(p.dec(result['metrics']['closed_net_usdt']),p.dec(result['metrics']['price_pnl_usdt'])-p.dec(result['metrics']['fees_usdt'])+p.dec(result['metrics']['funding_usdt']))
 def test_missing_universe_frame_not_shrunk(self):
  d=dataset();del d['frames'][0]['books']['ETH/USDT'];result=replay.replay(d,self.path)
  self.assertEqual(result['status'],'INVALID_DECLARED_GAP');self.assertIn('UNIVERSE',result['gaps'][0]['gap'])
 def test_future_history_no_lookahead(self):
  d=dataset();d['frames'][0]['history']['BTC-USDT-SWAP'][0]['fundingTime']=str(d['frames'][0]['at_ms']+1)
  with self.assertRaisesRegex(ValueError,'CAUSALITY'):replay.validate_frame(d,d['frames'][0])
 def test_missing_realized_rate_not_prediction(self):
  d=dataset()
  for row in d['frames'][-2]['history']['ETH-USDT-SWAP']:row.pop('realizedRate')
  result=replay.replay(d,self.path);self.assertEqual(result['status'],'INVALID_DECLARED_GAP');self.assertIsNone(result['metrics']['closed_net_usdt'])
 def test_complete_linked_cycle_can_pass_declared_observation(self):
  import recovery_a
  d=dataset();a.time.time.return_value=d['frames'][0]['at_ms']/1000-1
  a.begin(self.path,self.state,'.001',30000,self.config);a.record(self.path,self.state,self.report())
  replay.replay(d,Path(self.tmp.name)/'replay')
  ledger=Path(self.tmp.name)/'replay/paper.json';state=json.loads(ledger.read_text())
  a.time.time.return_value=d['frames'][-1]['at_ms']/1000+1
  # 模拟最后一笔平仓已原子记账、阶段写入中断，使用正式恢复路径收尾。
  last=list(state['trades'].values())[-1];last['phase']='closing';p.save(ledger,state)
  self.assertEqual(recovery_a.run(state,ledger)['status'],'RECOVERED')
  a.record(self.path,state,self.report());result=a.assess(self.path)
  self.assertEqual(result['status'],'PASS_PAPER_OBSERVATION',result)
  self.assertTrue(result['events']['complete_cycle']);self.assertTrue(result['events']['recovery'])
if __name__=='__main__':unittest.main(verbosity=2)
