import copy,json,sys,unittest
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[2]))
import unequal_hedge as u
import ccxt
T=1789416000000
MARKETS={}
for sym in ('BTC/USDT','BTC/USDT:USDT'):
 swap=':' in sym
 MARKETS[sym]=dict(id='BTC-USDT-SWAP' if swap else 'BTC-USDT',symbol=sym,active=True,base='BTC',quote='USDT',spot=not swap,swap=swap,linear=swap,inverse=False,settle='USDT' if swap else None,contractSize=.1 if swap else 1,precision={'amount':.001,'price':.01},limits={})
def frame(at,price=100):
 bars=[]
 for i in range(100):
  c=102-i*.02;bars.append([T-(100-i)*60000,str(c),str(c+.03),str(c-.03),str(c),'1','1','1','1'])
 return dict(at_ms=at,books={s:{'timestamp':at,'asks':[[str(u.dec(price)+u.dec(".01")),10000]],'bids':[[price,10000]]} for s in MARKETS},candles={'BTC':bars},funding={'BTC/USDT:USDT':{'fundingRate':'.0001','fundingTimestamp':T+28800000}},history={'BTC-USDT-SWAP':[{'instId':'BTC-USDT-SWAP','fundingTime':str(T-28800000),'realizedRate':'.0001'}]},mark_candles={'BTC-USDT-SWAP':[]})
class Check(unittest.TestCase):
 def setUp(self):
  self.cfg=copy.deepcopy(u.CONFIG);self.cfg['coins']=['BTC'];self.s=u.initial(self.cfg,T)
 def tick(self,t,p=100):
  result=u.advance(self.s,frame(t,p),MARKETS,ccxt.TICK_SIZE);u.audit(self.s);return result
 def opened(self):self.tick(T);self.tick(T+2000)
 def test_no_same_frame_entry_and_identical_baselines(self):
  self.tick(T)
  for ratio,a in self.s['groups'].items():
   self.assertEqual(len(a['events']),2 if ratio=='1' else 3)
   self.assertEqual(a['meta']['BTC']['extra'],'0')
  self.tick(T+2000)
  for ratio,a in self.s['groups'].items():
   if ratio!='1':self.assertGreater(u.dec(a['meta']['BTC']['extra']),0)
  self.assertEqual(self.s['groups']['1']['meta']['BTC']['extra'],'0')
 def test_stop_next_frame_adverse_price_and_cooldown(self):
  self.opened();a=self.s['groups']['2'];base=a['meta']['BTC']['base_contracts']
  self.tick(T+4000,101);self.assertEqual(a['pending_actions']['BTC']['reason'],'STOP')
  self.tick(T+6000,102)
  self.assertEqual(u.dec(a['meta']['BTC']['extra']),0);self.assertEqual(u.position(a,'BTC/USDT:USDT'),u.dec(base))
  self.assertEqual(a['events'][-1]['order']['quote']['vwap'],'102.01')
  self.tick(T+8000,99);self.assertFalse(a['pending_actions'])
 def test_trailing_never_widens(self):
  self.opened();a=self.s['groups']['2'];old=u.dec(a['meta']['BTC']['stop'])
  self.tick(T+4000,99.7);new=u.dec(a['meta']['BTC']['stop']);self.assertLess(new,old)
  self.tick(T+6000,99.5);self.assertLessEqual(u.dec(a['meta']['BTC']['stop']),new)
 def test_restart_preserves_stop_pending(self):
  self.opened();self.tick(T+4000,101);self.s=json.loads(json.dumps(self.s));self.tick(T+6000,102)
  self.assertEqual(u.dec(self.s['groups']['2']['meta']['BTC']['extra']),0)
 def test_future_unconfirmed_candle_not_used(self):
  f=frame(T);a=u.signals(f['candles']['BTC'],T,self.cfg)
  f['candles']['BTC'].append([str(T),'1','2','1','2','1','1','1','0'])
  self.assertEqual(a,u.signals(f['candles']['BTC'],T,self.cfg))
 def test_missing_candle_invalid(self):
  f=frame(T);f['candles']['BTC'].pop(-20)
  with self.assertRaisesRegex(ValueError,'INTERVAL'):u.signals(f['candles']['BTC'],T,self.cfg)
 def test_gap_closes_overlay_and_nulls_comparison(self):
  self.opened();r=self.tick(T+20000)
  self.assertEqual(self.s['groups']['2']['pending_actions']['BTC']['reason'],'DATA_GAP_EXIT')
  self.assertIsNone(r['rows'][1]['net_pnl'])
 def test_negative_rate_cancels_pending_entry(self):
  self.tick(T);f=frame(T+2000);f['funding']['BTC/USDT:USDT']['fundingRate']='-.001'
  u.advance(self.s,f,MARKETS,ccxt.TICK_SIZE)
  self.assertEqual(u.dec(self.s['groups']['2']['meta']['BTC']['extra']),0)
 def test_fee_drag_comparison(self):
  self.opened();r=self.tick(T+4000)
  self.assertLess(u.dec(r['rows'][-1]['excess_vs_1x']),u.dec(r['rows'][1]['excess_vs_1x']))
 def test_budget_rejects_high_ratio_without_shrinking_stop(self):
  self.cfg['max_trade_risk']='5';self.s=u.initial(self.cfg,T);self.opened()
  self.assertEqual(self.s['groups']['3']['meta']['BTC']['extra'],'0')
  self.assertGreater(u.dec(self.s['groups']['1.25']['meta']['BTC']['extra']),0)
 def test_funding_quantity_and_idempotency(self):
  self.opened();f=frame(T+70000);at=T+4000;sym='BTC/USDT:USDT';inst='BTC-USDT-SWAP'
  f['history'][inst].append(dict(instId=inst,fundingTime=str(at),realizedRate='.001'))
  f['mark_candles'][inst]=[[str(at),'100','101','99','100','1']]
  ex=u.ReplayExchange({'markets':MARKETS,'precision_mode':ccxt.TICK_SIZE},f)
  u.settle_all(self.s,f,ex);key=sym+':'+str(at)
  one=u.dec(self.s['groups']['1']['funding'][key]['payment_usdt']);two=u.dec(self.s['groups']['2']['funding'][key]['payment_usdt'])
  self.assertEqual(two,2*one)
  old=copy.deepcopy(self.s);u.settle_all(self.s,f,ex);self.assertEqual(old,self.s)
 def test_partial_stop_remainder_is_kept(self):
  self.opened();self.tick(T+4000,101);f=frame(T+6000,102)
  f['books']['BTC/USDT:USDT']['asks'][0][1]=1
  r=u.advance(self.s,f,MARKETS,ccxt.TICK_SIZE)
  a=self.s['groups']['2'];self.assertGreater(u.dec(a['meta']['BTC']['extra']),0)
  self.assertIn('BTC',a['pending_actions']);self.assertIsNone(r['rows'][2]['liquidation_value'])
  self.tick(T+8000,103);self.assertEqual(u.dec(a['meta']['BTC']['extra']),0)
 def test_daily_kill_and_no_martingale(self):
  self.cfg['daily_loss']='1';self.s=u.initial(self.cfg,T);self.opened();self.tick(T+4000,100.1)
  a=self.s['groups']['3'];self.assertTrue(a['daily_halted']);self.assertIn('BTC',a['pending_actions'])
if __name__=='__main__':unittest.main(verbosity=2)
