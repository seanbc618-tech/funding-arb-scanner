"""F05 算术及缺口验收，全为离线样本。"""
import copy
import hashlib
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import time
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import portfolio_a as v
import accounting as a

NOW=1800000000000

def fixture(partial=False,closed=False):
    p=dict(live=True,spot='BTC/USDT',perp='BTC/USDT:USDT',size=1,base=2,contracts=2,
           baseline=0,phase='open',opened_at=(NOW-10000000)/1000,orders=[])
    entries=[('base','buy',2,100,'.2'),('contracts','sell',2,102,'.2')]
    if partial or closed:
        q=2 if closed else 1
        entries += [('base','sell',q,110,str(q*.11)),('contracts','buy',q,108,str(q*.108))]
        p.update(base=2-q,contracts=2-q)
    if closed:p.update(phase='closed',closed_at=(NOW-4000000)/1000)
    fills=[];bills=[]
    for i,(leg,side,qty,price,fee) in enumerate(entries):
        inst='BTC-USDT' if leg=='base' else 'BTC-USDT-SWAP'
        p['orders'].append(dict(leg=leg,side=side,result={'id':str(i),'filled':qty,'fee':{'currency':'USDT','cost':fee}}))
        fills.append(dict(billId=str(10+i),ordId=str(i),side=side,fillSz=str(qty),fillPx=str(price),
                          fee=str(-a.dec(fee)),feeCcy='USDT',instId=inst,ts=str(NOW-9000000+i*100)))
        bills.append(dict(billId=str(20+i),type='2',ordId=str(i),instId=inst,ts=fills[-1]['ts']))
    bills.append(dict(billId='30',type='8',ccy='USDT',balChg='.5',instId='BTC-USDT-SWAP',ts=str(NOW-5000000)))
    return p,fills,bills


def quote(ex,symbol,side,qty):
    return {'mid_price':110 if side=='sell' else 111,'vwap':109 if side=='sell' else 112,
            'book_timestamp':int(time.time()*1000)}

class Portfolio(unittest.TestCase):
    def setUp(self):
        self.ex=Mock();self.ex.fetch_trading_fee.return_value={'taker':.001}
    def value(self,partial=False,closed=False):
        p,f,b=fixture(partial,closed);old=copy.deepcopy((p,f,b))
        ledger=v.pnl(p,f,b,NOW)
        with patch.object(v,'execution',side_effect=quote): r=v.value_position(self.ex,p,ledger)
        self.assertEqual((p,f,b),old)
        json.dumps(r,allow_nan=False)
        return r
    def test_open_arithmetic(self):
        r=self.value();self.assertEqual(a.dec(r['unrealized_pnl_usdt']),2)
        self.assertEqual(a.dec(r['estimated_net_pnl_usdt']),a.dec('2.1'))
        self.assertEqual(a.dec(r['exit_spread_slippage_usdt']),4)
        self.assertEqual(a.dec(r['exit_fee_estimate_usdt']),a.dec('.442'))
        self.assertEqual(a.dec(r['estimated_net_after_exit_usdt']),a.dec('-2.342'))
        self.assertIsNone(r['settled_net_pnl_usdt'])
    def test_partial_realized_unrealized(self):
        r=self.value(partial=True)
        self.assertEqual(a.dec(r['realized_trade_pnl_usdt']),4)
        self.assertEqual(a.dec(r['unrealized_pnl_usdt']),1)
        self.assertEqual(a.dec(r['estimated_net_pnl_usdt']),a.dec('4.882'))
        self.assertEqual(a.dec(r['estimated_net_after_exit_usdt']),a.dec('2.661'))
    def test_closed_matches_existing_accounting(self):
        p,f,b=fixture(closed=True);r=self.value(closed=True)
        self.assertEqual(r['settled_net_pnl_usdt'],a.summarize(p,f,b,NOW)['net_pnl_usdt'])
        self.assertEqual(r['exit_total_cost_usdt'],'0')
    def test_missing_coverage_null_not_zero(self):
        p,f,b=fixture();r=v.pnl(p,f,b,NOW,['QUERY_COVERAGE_MISSING'])
        self.assertIsNone(r['booked_funding_usdt']);self.assertIsNone(r['realized_trade_pnl_usdt'])
    def test_pending_null(self):
        p,f,b=fixture();p['pending']={'id':'x'}
        self.assertIsNone(v.pnl(p,f,b,NOW)['realized_trade_pnl_usdt'])
    def test_unknown_order_no_attribution(self):
        p,f,b=fixture();f[0]['ordId']='foreign'
        self.assertIn('UNATTRIBUTED_FILL',v.pnl(p,f,b,NOW)['gaps'])
    def test_wrong_quantity_blocks(self):
        p,f,b=fixture();p['base']=1
        r=v.pnl(p,f,b,NOW);self.assertIsNone(r['realized_trade_pnl_usdt'])
        self.assertIn('CONFIRMED_REMAINDER_MISMATCH',r['gaps'])
    def test_no_funding_is_zero_only_with_coverage(self):
        p,f,b=fixture();r=v.pnl(p,f,b[:-1],NOW)
        self.assertEqual(r['booked_funding_usdt'],'0')
    def test_negative_funding_preserved(self):
        p,f,b=fixture();b[-1]['balChg']='-.5'
        self.assertEqual(v.pnl(p,f,b,NOW)['booked_funding_usdt'],'-0.5')
    def test_non_usdt_fee_keeps_currency(self):
        p,f,b=fixture();f[0].update(feeCcy='BTC',fee='-.01')
        p['orders'][0]['result']['fee']={'currency':'BTC','cost':'.01'};p['base']=1.99
        r=v.pnl(p,f,b,NOW)
        self.assertEqual(r['fees_by_currency']['BTC'],'0.01')
        self.assertEqual(r['realized_trade_pnl_usdt'],'0')
        self.assertEqual(r['remaining_spot_cost_usdt'],'200')
    def test_unknown_exit_fee_preserves_mark_pnl(self):
        self.ex.fetch_trading_fee.side_effect=TimeoutError()
        r=self.value();self.assertIsNotNone(r['unrealized_pnl_usdt']);self.assertIsNone(r['exit_total_cost_usdt'])
    def test_bad_quote_no_estimate(self):
        p,f,b=fixture()
        with patch.object(v,'execution',side_effect=ValueError('depth')):
            r=v.value_position(self.ex,p,v.pnl(p,f,b,NOW))
        self.assertIsNone(r['unrealized_pnl_usdt']);self.assertIsNotNone(r['booked_funding_usdt'])
    def test_stale_quote_no_estimate(self):
        p,f,b=fixture()
        with patch.object(v,'execution',return_value={'book_timestamp':0}):
            r=v.value_position(self.ex,p,v.pnl(p,f,b,NOW))
        self.assertIsNone(r['estimated_net_after_exit_usdt'])
    def test_dry_not_real_pnl(self):
        p,f,b=fixture();p['live']=False
        self.assertIsNone(v.pnl(p,f,b,NOW)['booked_funding_usdt'])
    def test_readonly_db_unchanged(self):
        p,f,b=fixture(closed=True)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'a.sqlite'
            with sqlite3.connect(path) as db:
                a.init_db(db);a.store(db,'a','fills',f);a.store(db,'a','bills',b)
                for k,s in [('fills','BTC-USDT'),('fills','BTC-USDT-SWAP'),('bills','ACCOUNT')]:
                    db.execute('INSERT INTO collection_windows VALUES (?,?,?,?,?,NULL,1,?)',
                               ('a',k,s,int(p['opened_at']*1000),int(p['closed_at']*1000),NOW))
            db.close()
            before=hashlib.sha256(path.read_bytes()).hexdigest()
            x,y,g=v.read_evidence(path,'a',p,{'BTC/USDT':{'id':'BTC-USDT'},'BTC/USDT:USDT':{'id':'BTC-USDT-SWAP'}},NOW)
            self.assertEqual((x,y,g),(f,b,[]));self.assertEqual(hashlib.sha256(path.read_bytes()).hexdigest(),before)
    def test_missing_db_does_not_create(self):
        p,_,_=fixture()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'missing';self.assertIn('EVIDENCE_DB_MISSING',v.read_evidence(path,'a',p,{},NOW)[2])
            self.assertFalse(path.exists())
    def test_account_equity_is_usd_not_strategy_nav(self):
        self.ex.private_get_account_config.return_value={'data':[{'uid':'offline','acctLv':'2','posMode':'net_mode'}]}
        self.ex.fetch_balance.return_value={'info':{'data':[{'totalEq':'1234','imr':'10','isoEq':'0'}]},'USDT':{'free':100}}
        self.ex.fetch_positions.return_value=[]
        r=v.report(self.ex,{},Path('/does-not-exist'))
        self.assertEqual(r['account']['equity_usd'],'1234')
        self.assertIsNone(r['strategy_nav_usdt']);self.assertIsNone(r['capital_return'])
        self.assertEqual(r['status'],'NO_STRATEGY_POSITIONS')
    def test_unknown_account_not_empty(self):
        self.ex.fetch_positions.side_effect=TimeoutError()
        self.ex.fetch_balance.side_effect=TimeoutError()
        self.ex.private_get_account_config.side_effect=TimeoutError()
        r=v.report(self.ex,{},Path('/does-not-exist'))
        self.assertEqual(r['status'],'DATA_GAP');self.assertIsNone(r['account']['unowned_position_count'])

    def test_base_fee_closed_matches_cash_accounting(self):
        p,f,b=fixture(closed=True)
        f[0].update(feeCcy='BTC',fee='-.01');p['orders'][0]['result']['fee']={'currency':'BTC','cost':'.01'}
        f[2]['fillSz']='1.99';p['orders'][2]['result']['filled']='1.99'
        r=v.pnl(p,f,b,NOW)
        self.assertEqual(r['settled_net_pnl_usdt'],a.summarize(p,f,b,NOW)['net_pnl_usdt'])
        computed=a.dec(r['realized_trade_pnl_usdt'])+a.dec(r['booked_funding_usdt'])-a.dec(r['fees_by_currency']['USDT'])
        self.assertEqual(computed,a.dec(r['settled_net_pnl_usdt']))

    def test_unknown_fee_currency_blocks_conversion(self):
        p,f,b=fixture();f[0]['feeCcy']='OTHER';p['orders'][0]['result']['fee']['currency']='OTHER'
        r=v.pnl(p,f,b,NOW)
        self.assertIn('UNPRICED_FEE_CURRENCY',r['gaps'])
        self.assertIsNone(r['realized_trade_pnl_usdt'])

    def test_report_end_rechecks_quote_age(self):
        p,f,b=fixture()
        self.ex.private_get_account_config.return_value={'data':[{'uid':'offline','acctLv':'2','posMode':'net_mode'}]}
        self.ex.fetch_balance.return_value={'info':{'data':[{'totalEq':'1000'}]},'USDT':{'free':100}}
        self.ex.fetch_positions.return_value=[]
        item={'gaps':[],'quotes':{'base':{'book_timestamp':NOW}},'estimated_net_pnl_usdt':'1',
              'unrealized_pnl_usdt':'1','estimated_net_after_exit_usdt':'1','exit_total_cost_usdt':'0'}
        with patch.object(v.time,'time',side_effect=[NOW/1000,(NOW+6000)/1000]), \
             patch.object(v,'read_evidence',return_value=(f,b,[])), \
             patch('trade_a.reconcile',return_value=(2,2)), \
             patch.object(v,'value_position',return_value=item):
            r=v.report(self.ex,{'BTC':p},Path('/unused'))
        self.assertIsNone(r['positions']['BTC']['estimated_net_pnl_usdt'])
        self.assertIn('VALUATION_QUOTES_STALE_AT_COMPLETION',r['positions']['BTC']['gaps'])

if __name__=='__main__':unittest.main(verbosity=2)
