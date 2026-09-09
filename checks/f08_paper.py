import sys,time,tempfile,copy,json
from pathlib import Path
from unittest.mock import patch
import unittest
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import paper_a as p

class Exchange:
    def __init__(self):
        self.markets={'BTC/USDT':{'active':True,'spot':True,'quote':'USDT','limits':{}},'BTC/USDT:USDT':{'active':True,'swap':True,'linear':True,'settle':'USDT','quote':'USDT','contractSize':.1,'limits':{}}}
        self.depth=100;self.fail=False
    def amount_to_precision(self,s,q):return str(p.dec(q).quantize(p.dec('.001'),rounding='ROUND_DOWN'))
    def price_to_precision(self,s,q):return str(p.dec(q).quantize(p.dec('.01'),rounding='ROUND_DOWN'))
    def fetch_ticker(self,s):return {'last':100}
    def fetch_order_book(self,s,limit=50):
        if self.fail and ':' in s:raise TimeoutError()
        return {'timestamp':int(time.time()*1000),'asks':[[100,self.depth],[100.1,self.depth],[101,100]],'bids':[[99.9,self.depth],[99.8,self.depth],[98,100]]}

class Paper(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'paper.json';self.s=p.initial(1000,'.001','.001');p.save(self.path,self.s);self.ex=Exchange()
    def test_buy_asks_sell_bids(self):
        self.assertEqual(p.quote(self.ex,'BTC/USDT','buy',1)['vwap'],'100')
        self.assertEqual(p.quote(self.ex,'BTC/USDT','sell',1)['vwap'],'99.9')
    def test_partial_and_price_cap(self):
        self.ex.depth=.2;q=p.quote(self.ex,'BTC/USDT','buy',1)
        self.assertEqual(q['filled'],'0.400');self.assertEqual(q['status'],'PARTIAL_CANCELED');self.assertEqual(p.dec(q['vwap']),p.dec('100.05'))
    def test_contract_units(self):
        q=p.quote(self.ex,'BTC/USDT:USDT','sell',10);self.assertEqual(p.dec(q['notional_usdt']),p.dec('99.9'))
    def test_stale_rejected(self):
        b=self.ex.fetch_order_book('x');b['timestamp']=0
        with patch.object(self.ex,'fetch_order_book',return_value=b):
            with self.assertRaisesRegex(ValueError,'STALE'):p.quote(self.ex,'BTC/USDT','buy',1)
    def test_crossed_rejected(self):
        b=self.ex.fetch_order_book('x');b['bids'][0][0]=101
        with patch.object(self.ex,'fetch_order_book',return_value=b):
            with self.assertRaises(ValueError):p.quote(self.ex,'BTC/USDT','buy',1)
    def test_full_cycle_fees_margin(self):
        p.operate(self.s,self.path,self.ex,'BTC',100)
        self.assertEqual(self.s['trades'][next(iter(self.s['trades']))]['phase'],'open')
        self.assertEqual(p.dec(self.s['positions']['BTC/USDT:USDT']['margin_usdt']),p.dec('99.9'))
        p.operate(self.s,self.path,self.ex,'BTC',closing=True)
        self.assertEqual(p.dec(self.s['cash_usdt']),p.dec('999.4002'))
        self.assertEqual(p.available(self.s),p.dec('999.4002'))
    def test_insufficient_pair_no_intent(self):
        self.s['cash_usdt']='100';before=copy.deepcopy(self.s)
        with self.assertRaisesRegex(ValueError,'CAPITAL'):p.operate(self.s,self.path,self.ex,'BTC',100)
        self.assertEqual(self.s,before)
    def test_reservation_durable_before_fill(self):
        original=p.apply_fill
        def apply(s,q):
            disk=json.loads(self.path.read_text());self.assertGreater(p.dec(disk['pending']['reserved_usdt']),0);original(s,q)
        with patch.object(p,'apply_fill',side_effect=apply):p.operate(self.s,self.path,self.ex,'BTC',100)
        self.assertIsNone(self.s['pending'])
    def test_second_leg_failure_preserves_first(self):
        original=p.submit
        def submit(s,path,ex,symbol,*args):
            if ':' in symbol:ex.fail=True
            return original(s,path,ex,symbol,*args)
        with patch.object(p,'submit',side_effect=submit):
            with self.assertRaises(TimeoutError):p.operate(self.s,self.path,self.ex,'BTC',100)
        self.assertEqual(p.dec(self.s['positions']['BTC/USDT']['quantity']),1)
        self.assertEqual(next(iter(self.s['trades'].values()))['phase'],'needs_close')
        with self.assertRaises(RuntimeError):p.operate(self.s,self.path,self.ex,'BTC',100)
    def test_partial_perp_close_keeps_spot(self):
        p.operate(self.s,self.path,self.ex,'BTC',100);self.ex.depth=1
        p.operate(self.s,self.path,self.ex,'BTC',closing=True)
        self.assertEqual(p.dec(self.s['positions']['BTC/USDT']['quantity']),1)
        self.assertEqual(p.dec(self.s['positions']['BTC/USDT:USDT']['quantity']),8)
    def test_pending_restart_never_resimulates(self):
        self.s['pending']={'reserved_usdt':'10'};p.save(self.path,self.s)
        with self.assertRaises(RuntimeError):p.operate(self.s,self.path,self.ex,'BTC',100)
        self.assertEqual(p.available(self.s),990)
    def test_init_finite_fee(self):
        for x in ('NaN','-1','1'):
            with self.assertRaises((ValueError,ArithmeticError)):p.initial(100,x,0)
    def test_precision_does_not_round_up(self):
        q=p.quote(self.ex,'BTC/USDT','buy','1.0009');self.assertEqual(q['requested'],'1.000')
    def test_overclose_rejected(self):
        self.s['trades']['x']={'orders':[]}
        with self.assertRaisesRegex(ValueError,'REDUCE_ONLY'):p.submit(self.s,self.path,self.ex,'BTC/USDT:USDT','buy',1,'x')
    def test_sell_reserve_covers_best_bid_not_lower_limit(self):
        self.s['trades']['x']={'orders':[]}
        self.s['cash_usdt']='99.8'
        with self.assertRaisesRegex(ValueError,'INSUFFICIENT_PAPER_FUNDS'):
            p.submit(self.s,self.path,self.ex,'BTC/USDT:USDT','sell',10,'x')
        self.assertIsNone(self.s['pending'])

    def test_sell_uses_all_depth_inside_cap(self):
        self.ex.depth=.2
        q=p.quote(self.ex,'BTC/USDT','sell',1)
        self.assertEqual(p.dec(q['filled']),p.dec('.4'))
        self.assertEqual(p.dec(q['vwap']),p.dec('99.85'))

    def test_spot_sale_can_pay_fee_from_proceeds(self):
        self.s['trades']['x']={'orders':[]}
        p.submit(self.s,self.path,self.ex,'BTC/USDT','buy','9.97','x')
        b=self.ex.fetch_order_book('x');b['asks']=[[301,100]];b['bids']=[[300,100]]
        with patch.object(self.ex,'fetch_order_book',return_value=b):
            p.submit(self.s,self.path,self.ex,'BTC/USDT','sell','9.97','x')
        self.assertEqual(p.dec(self.s['cash_usdt']),p.dec('2990.012'))

    def test_perp_close_can_use_released_margin(self):
        self.s=p.initial('100','.001','.001');self.s['trades']['x']={'orders':[]}
        p.submit(self.s,self.path,self.ex,'BTC/USDT:USDT','sell',10,'x')
        self.assertEqual(p.available(self.s),p.dec('.0001'))
        p.submit(self.s,self.path,self.ex,'BTC/USDT:USDT','buy',10,'x')
        self.assertEqual(p.available(self.s),p.dec('99.7001'))

    def test_perp_loss_cannot_spend_unfunded_cash(self):
        self.s=p.initial('100.5999','.001','.001');self.s['trades']['x']={'orders':[]}
        p.submit(self.s,self.path,self.ex,'BTC/USDT:USDT','sell',10,'x')
        before=copy.deepcopy(self.s)
        b=self.ex.fetch_order_book('x');b['asks']=[[250,100]];b['bids']=[[249,100]]
        with patch.object(self.ex,'fetch_order_book',return_value=b):
            with self.assertRaisesRegex(ValueError,'INSUFFICIENT_PAPER_FUNDS'):
                p.submit(self.s,self.path,self.ex,'BTC/USDT:USDT','buy',10,'x')
        self.assertEqual(self.s,before)

    def test_atomic_fill_failure_keeps_pending(self):
        original=p.save;count=[0]
        def save(path,state):
            if state['trades'] and next(iter(state['trades'].values()))['orders']:
                raise OSError('disk')
            original(path,state)
        with patch.object(p,'save',side_effect=save):
            with self.assertRaises(OSError):p.operate(self.s,self.path,self.ex,'BTC',100)
        self.assertIsNotNone(json.loads(self.path.read_text())['pending'])

if __name__=='__main__':unittest.main(verbosity=2)
