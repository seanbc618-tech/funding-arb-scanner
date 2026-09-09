import copy,json,sys,tempfile,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import paper_a as p
import paper_funding as f

T=1788912000000
S='BTC/USDT:USDT'
class Exchange:
    markets={S:{'id':'BTC-USDT-SWAP','swap':True,'linear':True,'settle':'USDT'}}
    def __init__(self):
        self.rate={'code':'0','data':[{'instId':'BTC-USDT-SWAP','fundingTime':str(T),'realizedRate':'0.001','fundingRate':'0.99'}]}
        self.price={'code':'0','data':[[str(T),'100','101','99','100','1']]}
    def publicGetPublicFundingRateHistory(self,params):
        assert params['before']==str(T-1) and params['after']==str(T+1)
        return self.rate
    def publicGetMarketHistoryMarkPriceCandles(self,params):return self.price

class Funding(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.path=Path(self.tmp.name)/'paper.json';self.s=p.initial(1000,'.001','.001');self.ex=Exchange()
        self.s['trades']['x']={'orders':[]};self.fill('sell',10,T-1000);p.save(self.path,self.s)
    def fill(self,side,qty,ts):
        q={'symbol':S,'kind':'perp','side':side,'filled':str(qty),'contract_size':'.1','notional_usdt':str(p.dec(qty)*10)}
        p.apply_fill(self.s,q)
        self.s['trades']['x']['orders'].append({'order_id':str(len(self.s['trades']['x']['orders'])),'trade_id':'x','simulated':True,'quote':q,'executed_at_ms':ts})
    def settle(self):return f.settle(self.s,self.path,self.ex,S,T,now=T+120000)
    def test_positive_short_credit_and_no_prediction(self):
        before=p.dec(self.s['cash_usdt']);r=self.settle()
        self.assertEqual(r['status'],'POSTED');self.assertEqual(p.dec(r['payment_usdt']),p.dec('.1'));self.assertEqual(p.dec(self.s['cash_usdt']),before+p.dec('.1'))
    def test_negative_rate_debit(self):
        self.ex.rate['data'][0]['realizedRate']='-.002';before=p.dec(self.s['cash_usdt']);self.settle();self.assertEqual(p.dec(self.s['cash_usdt']),before-p.dec('.2'))
    def test_restart_duplicate_no_fetch_or_payment(self):
        self.settle();self.s=json.loads(self.path.read_text());before=copy.deepcopy(self.s)
        with patch.object(self.ex,'publicGetPublicFundingRateHistory',side_effect=AssertionError):self.settle()
        self.assertEqual(self.s,before)
    def test_partial_close_before_cycle(self):
        self.fill('buy',4,T-500);self.assertEqual(p.dec(self.settle()['payment_usdt']),p.dec('.06'))
    def test_closed_now_held_at_cycle(self):
        self.fill('buy',10,T+500);self.assertEqual(p.dec(self.settle()['payment_usdt']),p.dec('.1'))
    def test_closed_before_no_fee_no_price_needed(self):
        self.fill('buy',10,T-500);self.ex.price['data']=[];r=self.settle();self.assertEqual(r['status'],'NO_POSITION');self.assertEqual(r['payment_usdt'],'0')
    def test_open_after_cycle_no_charge(self):
        self.s['trades']['x']['orders'][0]['executed_at_ms']=T+500;self.assertEqual(self.settle()['status'],'NO_POSITION')
    def test_boundary_uncertain(self):
        self.s['trades']['x']['orders'][0]['executed_at_ms']=T;self.assertEqual(self.settle()['gap'],'SETTLEMENT_BOUNDARY_AMBIGUOUS')
    def test_legacy_timestamp_not_invented(self):
        del self.s['trades']['x']['orders'][0]['executed_at_ms'];self.assertEqual(self.settle()['gap'],'LEGACY_FILL_TIME_MISSING')
    def test_missing_realized_does_not_use_predicted(self):
        del self.ex.rate['data'][0]['realizedRate'];r=self.settle();self.assertIsNone(r['payment_usdt']);self.assertEqual(r['gap'],'REALIZED_RATE_MISSING')
    def test_missing_candle_can_resolve_once(self):
        original=copy.deepcopy(self.ex.price);self.ex.price['data']=[];before=self.s['cash_usdt'];self.assertIsNone(self.settle()['payment_usdt']);self.assertEqual(self.s['cash_usdt'],before)
        self.ex.price=original;self.assertEqual(self.settle()['status'],'POSTED');after=self.s['cash_usdt'];self.settle();self.assertEqual(self.s['cash_usdt'],after)
    def test_unconfirmed_or_wrong_timestamp_candle(self):
        self.ex.price['data'][0][5]='0';self.assertIsNone(self.settle()['payment_usdt'])
        self.ex.price['data'][0][5]='1';self.ex.price['data'][0][0]=str(T-60000);self.assertIsNone(self.settle()['payment_usdt'])
    def test_empty_history_declares_gap(self):
        self.ex.rate['data']=[];self.assertEqual(self.settle()['gap'],'SETTLED_RATE_MISSING_OR_DUPLICATE')
    def test_pending_or_history_mismatch_blocks(self):
        self.s['pending']={'reserved_usdt':'1'};self.assertEqual(self.settle()['gap'],'PENDING_EXECUTION')
        self.s['pending']=None;self.s['positions'][S]['quantity']='9';self.assertEqual(self.settle()['gap'],'POSITION_HISTORY_MISMATCH')
    def test_network_gap_preserves_cash(self):
        before=self.s['cash_usdt']
        with patch.object(self.ex,'publicGetPublicFundingRateHistory',side_effect=TimeoutError('private text')):
            r=self.settle()
        self.assertEqual(r['gap'],'PUBLIC_DATA_ERROR:TimeoutError');self.assertEqual(self.s['cash_usdt'],before)
    def test_atomic_failure_no_memory_credit(self):
        before=copy.deepcopy(self.s)
        with patch.object(f,'save',side_effect=OSError('disk')):
            with self.assertRaises(OSError):self.settle()
        self.assertEqual(self.s,before);self.assertEqual(json.loads(self.path.read_text()),before)
    def test_zero_rate_dedup(self):
        self.ex.rate['data'][0]['realizedRate']='0';self.settle();before=copy.deepcopy(self.s);self.settle();self.assertEqual(self.s,before)
    def test_market_load_failure_is_persisted_gap(self):
        self.ex.markets=None
        self.ex.load_markets=lambda: (_ for _ in ()).throw(TimeoutError())
        self.assertEqual(self.settle()['gap'],'PUBLIC_DATA_ERROR:TimeoutError')
        self.assertEqual(json.loads(self.path.read_text())['funding'][f'{S}:{T}']['payment_usdt'],None)

    def test_new_f08_fill_has_durable_execution_time(self):
        from f08_paper import Exchange as Books
        self.s=p.initial(1000,'.001','.001');p.save(self.path,self.s)
        p.operate(self.s,self.path,Books(),'BTC',100)
        disk=json.loads(self.path.read_text())
        orders=next(iter(disk['trades'].values()))['orders']
        self.assertEqual(len(orders),2)
        self.assertTrue(all(isinstance(o['executed_at_ms'],int) and o['executed_at_ms']>0 for o in orders))

    def test_commit_then_error_restart_does_not_credit_twice(self):
        original=f.save
        def committed_then_error(path,state):
            original(path,state)
            raise OSError('after replace')
        before=self.s['cash_usdt']
        with patch.object(f,'save',side_effect=committed_then_error):
            with self.assertRaises(OSError):self.settle()
        self.assertEqual(self.s['cash_usdt'],before)
        self.s=json.loads(self.path.read_text());after=self.s['cash_usdt']
        self.assertEqual(p.dec(after)-p.dec(before),p.dec('.1'))
        self.settle();self.assertEqual(self.s['cash_usdt'],after)

    def test_duplicate_fill_declares_gap(self):
        self.s['trades']['x']['orders'].append(copy.deepcopy(self.s['trades']['x']['orders'][0]))
        self.assertEqual(self.settle()['gap'],'DUPLICATE_ORDER_ID')

    def test_between_two_holding_periods_no_fee(self):
        self.fill('buy',10,T-500);self.fill('sell',5,T+500)
        self.assertEqual(self.settle()['status'],'NO_POSITION')

    def test_distinct_cycles_credit_once_each(self):
        self.settle();before=p.dec(self.s['cash_usdt']);later=T+3600000
        row=copy.deepcopy(self.ex.rate['data'][0]);row['fundingTime']=str(later)
        candle=copy.deepcopy(self.ex.price['data'][0]);candle[0]=str(later)
        with patch.object(self.ex,'publicGetPublicFundingRateHistory',return_value={'code':'0','data':[row]}), patch.object(self.ex,'publicGetMarketHistoryMarkPriceCandles',return_value={'code':'0','data':[candle]}):
            f.settle(self.s,self.path,self.ex,S,later,now=later+120000)
            f.settle(self.s,self.path,self.ex,S,later,now=later+120000)
        self.assertEqual(p.dec(self.s['cash_usdt'])-before,p.dec('.1'))
        self.assertEqual(len(self.s['funding']),2)

    def test_future_rejected_without_mutation(self):
        before=copy.deepcopy(self.s)
        with self.assertRaisesRegex(ValueError,'NOT_READY'):f.settle(self.s,self.path,self.ex,S,T,now=T)
        self.assertEqual(self.s,before)

if __name__=='__main__':unittest.main(verbosity=2)
