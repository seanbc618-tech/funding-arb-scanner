import sys,time,copy,unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import rank_a as r
from f08_paper import Exchange as Books
N=1788915600000
IV=28800000
class Exchange(Books):
    def __init__(self):
        super().__init__();self.markets['BTC/USDT:USDT']['id']='BTC-USDT-SWAP'
        self.rate='.001';self.fee_spot='.001';self.fee_perp='.0005'
        self.history=[{'instId':'BTC-USDT-SWAP','fundingTime':str(1788912000000-i*IV),'realizedRate':'.001'} for i in range(23)]
    def fetch_trading_fee(self,s):return {'symbol':s,'taker':self.fee_perp if ':' in s else self.fee_spot}
    def fetch_funding_rate(self,s):return {'info':{'instId':'BTC-USDT-SWAP','method':'current_period','fundingTime':str(1788912000000+IV),'nextFundingTime':str(1788912000000+2*IV),'fundingRate':self.rate}}
    def publicGetPublicFundingRateHistory(self,params):return {'code':'0','data':self.history}

class Rank(unittest.TestCase):
    def setUp(self):
        self.ex=Exchange();self.clock=patch('time.time',return_value=N/1000);self.clock.start();self.addCleanup(self.clock.stop)
        self.kw=dict(notional=1000,hold_hours=720,margin_ratio=1,reserve_usdt=100,basis_stress_bps=50)
    def evaluate(self):return r.evaluate(self.ex,'BTC',**self.kw)
    def test_amount_cost_capital_arithmetic(self):
        x=self.evaluate();self.assertEqual(x['status'],'CANDIDATE')
        self.assertEqual(r.dec(x['roundtrip_cost_usdt']),r.dec('4.9985'))
        self.assertEqual(r.dec(x['expected_net_usdt']),r.dec('84.9565'))
        self.assertEqual(r.dec(x['capital_usdt']),r.dec('2100.4995'))
        self.assertEqual(r.dec(x['capital_return']),r.dec('84.9565')/r.dec('2100.4995'))
    def test_short_hold_high_apr_rejected(self):
        self.kw['hold_hours']=24;x=self.evaluate();self.assertEqual(x['status'],'REJECT_NET');self.assertGreater(r.dec(x['historical_apr']),1)
    def test_hold_before_next_cycle_no_funding(self):
        self.kw['hold_hours']=1;x=self.evaluate();self.assertEqual(x['forecast_cycles'],0);self.assertEqual(r.dec(x['expected_funding_usdt']),0)
    def test_negative_current_conservative(self):
        self.ex.rate='-.001';x=self.evaluate();self.assertLess(r.dec(x['expected_funding_usdt']),0);self.assertIsNone(x['breakeven_days'])
    def test_predictive_spike_capped_by_history(self):
        self.ex.rate='.1';x=self.evaluate();self.assertEqual(r.dec(x['forecast_rate_per_cycle']),r.dec('.001'))
    def test_stress_separate_from_expected(self):
        x=self.evaluate();self.kw['basis_stress_bps']=100;y=self.evaluate();self.assertEqual(x['expected_net_usdt'],y['expected_net_usdt']);self.assertEqual(r.dec(y['stressed_net_usdt']),r.dec(y['expected_net_usdt'])-10)
    def test_no_fee_fallback(self):
        self.ex.fee_spot=None;x=self.evaluate();self.assertEqual(x['status'],'INVALID_DECLARED_GAP');self.assertIsNone(x['expected_net_usdt'])
    def test_insufficient_depth_excluded(self):
        self.ex.depth=.01;x=self.evaluate();self.assertEqual(x['status'],'NOT_EXECUTABLE');self.assertIsNone(x['expected_net_usdt'])
    def test_history_gap_not_interpolated(self):
        self.ex.history.pop(2);self.assertEqual(self.evaluate()['reason'],'HISTORY_GAP_OR_INTERVAL_CHANGED')
    def test_truncated_history_not_shortened(self):
        self.ex.history=self.ex.history[:2];self.assertEqual(self.evaluate()['status'],'INVALID_DECLARED_GAP')
    def test_invalid_scenario_null(self):
        self.kw['reserve_usdt']=-1;self.assertIsNone(self.evaluate()['capital_return'])
    def test_break_even_uses_cycles(self):
        x=self.evaluate();self.assertEqual(r.dec(x['breakeven_days']),r.dec(7+5*8)/24)
    def test_rank_total_capital_and_keep_rejections(self):
        rows={'BTC':{'coin':'BTC','status':'CANDIDATE','capital_return':'.01','expected_net_usdt':'100','rank':None},'ETH':{'coin':'ETH','status':'CANDIDATE','capital_return':'.02','expected_net_usdt':'50','rank':None},'SOL':{'coin':'SOL','status':'REJECT_NET','rank':None}}
        with patch.object(r,'evaluate',side_effect=lambda ex,c,**kw:copy.deepcopy(rows[c])):
            x=r.rank(self.ex,['BTC','ETH','SOL'],**self.kw)
        self.assertEqual([x['coin'] for x in x['rows']],['ETH','BTC','SOL']);self.assertIsNone(x['rows'][2]['rank'])
    def test_cross_period_method_not_assumed(self):
        current=self.ex.fetch_funding_rate('x');current['info']['method']='next_period'
        with patch.object(self.ex,'fetch_funding_rate',return_value=current):
            self.assertEqual(self.evaluate()['reason'],'UNSUPPORTED_FUNDING_CONTRACT_OR_METHOD')
    def test_conflicting_history_not_silently_deduped(self):
        row=copy.deepcopy(self.ex.history[0]);row['realizedRate']='.5';self.ex.history.append(row)
        self.assertEqual(self.evaluate()['reason'],'CONFLICTING_HISTORY')
    def test_empty_eligible_hold_cash(self):
        self.kw['hold_hours']=1;self.assertEqual(r.rank(self.ex,['BTC'],**self.kw)['decision'],'HOLD_CASH')

if __name__=='__main__':unittest.main(verbosity=2)
