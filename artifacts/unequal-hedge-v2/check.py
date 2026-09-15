import copy,importlib.util,sys,unittest
from pathlib import Path
from unittest.mock import patch
root=Path(__file__).resolve().parents[2];sys.path.insert(0,str(root))
import unequal_hedge as u
import unequal_signals_v2 as v
spec=importlib.util.spec_from_file_location('fixture',root/'artifacts/unequal-hedge-v1/check.py');f=importlib.util.module_from_spec(spec);spec.loader.exec_module(f)
T=f.T
class Check(unittest.TestCase):
 def setUp(self):
  self.cfg={**u.V2_CONFIG,'coins':['BTC']};self.s=u.initial(self.cfg,T)
  self.sig=dict(at=T,short=True,exit=False,reset=False,regime_at=T,regime=True,entry_cross=True,atr='.1',support='90')
 def tick(self,at,price=100):
  with patch.object(u,'signals',return_value=copy.deepcopy(self.sig)):
   r=u.advance(self.s,f.frame(at,price),f.MARKETS,f.ccxt.TICK_SIZE)
  u.audit(self.s);return r
 def open(self):self.tick(T);self.tick(T+2000)
 def bars(self):
  rows=[]
  for i in range(300):
   c=200-i*.3;rows.append([T-(300-i)*300000,str(c),str(c+.2),str(c-.2),str(c),'1'])
  for j,c in enumerate([111,112,113,114,115,111]):rows[-6+j][1:5]=[str(c),str(c+.2),str(c-.2),str(c)]
  return rows
 def test_interleaved_funding_replays_in_order(self):
  self.open()
  for a in self.s['groups'].values():
   amount='0.12345678901234567890123';key='test-cycle'
   a['cash_usdt']=str(u.dec(a['cash_usdt'])+u.dec(amount))
   a['funding'][key]={'status':'POSTED','payment_usdt':amount}
   u.log(a,{'type':'FUNDING','key':key,'payment_usdt':amount,'at_ms':T+2500})
  self.tick(T+4000,101);self.tick(T+6000,102);u.audit(self.s)
  self.s['groups']['2']['events'].append({'type':'FUNDING','key':'test-cycle','payment_usdt':amount})
  with self.assertRaisesRegex(ValueError,'FUNDING_EVENT_MISMATCH'):u.audit(self.s)
 def test_actual_slow_bear_and_pullback_cross(self):
  s=v.signals(self.bars(),T,self.cfg);self.assertTrue(s['short']);self.assertFalse(s['exit']);self.assertFalse(s['reset'])
 def test_unfinished_candle_cannot_change_signal(self):
  rows=self.bars();old=v.signals(rows,T,self.cfg);rows.append([T,'300','400','200','400','0']);self.assertEqual(old,v.signals(rows,T,self.cfg))
 def test_missing_bar_rejected(self):
  rows=self.bars();rows.pop(-30)
  with self.assertRaisesRegex(ValueError,'INTERVAL_GAP'):v.signals(rows,T,self.cfg)
 def test_small_bounce_no_trend_exit(self):
  self.open();self.sig['short']=False;self.tick(T+4000)
  self.assertFalse(self.s['groups']['2']['pending_actions']);self.assertGreater(u.dec(self.s['groups']['2']['meta']['BTC']['extra']),0)
 def test_confirmed_exit_still_works(self):
  self.open();self.sig.update(short=False,exit=True);self.tick(T+4000)
  self.assertEqual(self.s['groups']['2']['pending_actions']['BTC']['reason'],'TREND_EXIT')
 def test_hard_stop_not_delayed_for_candles(self):
  self.open();self.tick(T+4000,101);self.assertEqual(self.s['groups']['2']['pending_actions']['BTC']['reason'],'STOP')
 def test_same_regime_cannot_reenter_after_cooldown(self):
  self.open();self.tick(T+4000,101);self.tick(T+6000,102)
  for a in self.s['groups'].values():a['meta']['BTC']['cooldown_until']=0
  self.sig['at']=T+120000;self.tick(T+8000)
  self.assertFalse(self.s['groups']['2']['pending_actions'])
  self.sig.update(reset=True,short=False,regime_at=T+10000);self.tick(T+10000)
  self.sig.update(reset=False,short=True,regime_at=T+12000);self.tick(T+12000)
  self.assertEqual(self.s['groups']['2']['pending_actions']['BTC']['kind'],'OPEN')
 def test_cost_gate_rejects_no_room(self):
  self.sig['support']='99.95';self.tick(T);self.assertFalse(self.s['groups']['2']['pending_actions'])
 def test_cost_rechecked_at_execution(self):
  self.tick(T);self.sig['support']='99.95';self.tick(T+2000);self.assertEqual(self.s['groups']['2']['meta']['BTC']['extra'],'0')
 def test_settlement_wait_blocks_entry_not_forced_exit(self):
  self.open()
  for a in self.s['groups'].values():a['funding']['pending']={'status':'INVALID_DECLARED_GAP','payment_usdt':None}
  self.tick(T+4000);self.assertFalse(self.s['groups']['2']['pending_actions'])
  self.tick(T+6000,101);self.assertEqual(self.s['groups']['2']['pending_actions']['BTC']['reason'],'STOP')
if __name__=='__main__':unittest.main(verbosity=2)
