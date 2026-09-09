import sys
sys.path.insert(0, '/Volumes/数据分区/资金费率套利')
import copy
import json
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest
from unittest.mock import patch, Mock
from funding_arb_regression import Exchange
import trade_a as t
import check_a as c
import accounting as a
import manage_a as m


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.ex = Exchange()
        self.p = t.plan(self.ex, 'ZEC', 200)
        self.ex.base = 2
        self.ex.short = 2
        self.p['phase'] = 'open'

    def test_hold(self):
        self.assertEqual(c.check(self.ex, self.p)['action'], 'HOLD')

    def test_negative_funding_exit(self):
        with patch.object(self.ex, 'fetch_funding_rate', return_value={'fundingRate': -.01, 'fundingTimestamp': time.time()*1000+60000}):
            self.assertEqual(c.check(self.ex, self.p)['action'], 'EXIT')

    def test_low_margin_exit(self):
        b = self.ex.fetch_balance()
        b['info']['data'][0]['mgnRatio'] = '2'
        with patch.object(self.ex, 'fetch_balance', return_value=b):
            self.assertIn('MARGIN_RATIO_LOW', c.check(self.ex, self.p)['reasons'])

    def test_liquidation_distance(self):
        pos = self.ex.fetch_positions()
        pos[0]['liquidationPrice'] = 105
        with patch.object(self.ex, 'fetch_positions', return_value=pos):
            self.assertIn('LIQUIDATION_DISTANCE_LOW', c.check(self.ex, self.p)['reasons'])

    def test_liquidation_unknown_blocks(self):
        pos = self.ex.fetch_positions()
        pos[0]['liquidationPrice'] = None
        with patch.object(self.ex, 'fetch_positions', return_value=pos):
            self.assertEqual(c.check(self.ex, self.p)['action'], 'BLOCK')

    def test_pending_and_account_mismatch_block(self):
        self.p['pending'] = {'client_id': 'x'}
        self.assertEqual(c.check(self.ex, self.p)['action'], 'BLOCK')
        del self.p['pending']
        self.ex.base += 1
        self.assertEqual(c.check(self.ex, self.p)['action'], 'BLOCK')

    def test_shallow_book_blocks(self):
        book = self.ex.fetch_order_book('ZEC/USDT')
        book['asks'] = [[100.01, .01]]
        with patch.object(self.ex, 'fetch_order_book', return_value=book):
            with self.assertRaisesRegex(ValueError, 'INSUFFICIENT_DEPTH'):
                c.execution(self.ex, 'ZEC/USDT', 'buy', 2)

    def test_old_book_blocks(self):
        book = self.ex.fetch_order_book('ZEC/USDT')
        book['timestamp'] -= 10000
        with patch.object(self.ex, 'fetch_order_book', return_value=book):
            self.assertEqual(c.check(self.ex, self.p)['action'], 'BLOCK')

    def test_crossed_book_blocks(self):
        book = self.ex.fetch_order_book('ZEC/USDT')
        book['bids'][0][0] = 101
        with patch.object(self.ex, 'fetch_order_book', return_value=book):
            with self.assertRaisesRegex(ValueError, 'CROSSED'):
                c.execution(self.ex, 'ZEC/USDT', 'sell', 2)

    def test_vwap_units_and_limit(self):
        book = self.ex.fetch_order_book('ZEC/USDT')
        book['asks'] = [[100.01, 1], [100.02, 1]]
        with patch.object(self.ex, 'fetch_order_book', return_value=book):
            r = c.execution(self.ex, 'ZEC/USDT', 'buy', 2)
        self.assertAlmostEqual(r['vwap'], 100.015)
        self.assertLessEqual(r['limit_price'], 100.01*1.002)

    def test_insufficient_cash_entry_blocks(self):
        self.ex.short = 0
        b = self.ex.fetch_balance()
        b['USDT']['free'] = 100
        with patch.object(self.ex, 'fetch_balance', return_value=b):
            r = c.check(self.ex, self.p, opening=True)
        self.assertEqual(r['action'], 'BLOCK')
        self.assertIn('INSUFFICIENT_FREE_USDT', r['reasons'])

    def test_exposure_exit(self):
        self.p['base'] = self.ex.base = 3
        self.assertIn('EXPOSURE_LIMIT', c.check(self.ex, self.p)['reasons'])


class ManagerTests(unittest.TestCase):
    def setUp(self):
        tmp = tempfile.TemporaryDirectory(); self.addCleanup(tmp.cleanup)
        patcher = patch.object(t, 'ACCOUNT_LOCK_ROOT', Path(tmp.name)); patcher.start(); self.addCleanup(patcher.stop)

    def test_end_to_end_live_exit_only_with_flag(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(t, 'ROOT', Path(tmp)):
            ex = Exchange()
            with patch.object(t, 'exchange', return_value=ex):
                t.LIVE = True
                t.do_open('ZEC', 200)
                with patch.object(ex, 'fetch_funding_rate', return_value={
                        'fundingRate': -.001, 'fundingTimestamp': int(time.time()*1000)+60000}):
                    self.assertEqual(m.run(False, True)['ZEC']['action'], 'EXIT')
                    self.assertEqual(len(ex.calls), 2)
                    self.assertTrue(m.run(True, True)['ZEC']['executed'])
                    self.assertEqual(t.load()['ZEC']['phase'], 'closed')
                    self.assertEqual(len(ex.calls), 4)

    def test_readonly_does_not_close(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(t, 'ROOT', Path(tmp)), \
             patch.object(t, 'load', return_value={'ZEC': {'phase': 'open', 'trade_id': 'd'*32}}), \
             patch.object(t, 'exchange', return_value=Mock()), \
             patch.object(m, 'check', return_value={'action': 'EXIT', 'gaps': []}), \
             patch.object(t, 'do_close') as close:
            m.run(False, True)
            close.assert_not_called()

    def test_live_exit_and_block(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(t, 'ROOT', Path(tmp)), \
             patch.object(t, 'load', return_value={'ZEC': {'phase': 'closed', 'trade_id': 'd'*32}}), \
             patch.object(t, 'exchange', return_value=Mock()), \
             patch.object(m, 'check', return_value={'action': 'EXIT', 'gaps': []}), \
             patch.object(t, 'do_close') as close:
            result = m.run(True, True)
            close.assert_called_once_with('ZEC', expected_trade_id='d'*32)
            self.assertTrue(result['ZEC']['executed'])
        with tempfile.TemporaryDirectory() as tmp, patch.object(t, 'ROOT', Path(tmp)), \
             patch.object(t, 'load', return_value={'ZEC': {'phase': 'open', 'trade_id': 'd'*32}}), \
             patch.object(t, 'exchange', return_value=Mock()), \
             patch.object(m, 'check', return_value={'action': 'BLOCK', 'gaps': ['missing']}), \
             patch.object(t, 'do_close') as close:
            m.run(True, True)
            close.assert_not_called()

    def test_failed_exit_requires_attention(self):
        with tempfile.TemporaryDirectory() as tmp, patch.object(t, 'ROOT', Path(tmp)), \
             patch.object(t, 'load', return_value={'ZEC': {'phase': 'closing', 'trade_id': 'd'*32}}), \
             patch.object(t, 'exchange', return_value=Mock()), \
             patch.object(m, 'check', return_value={'action': 'EXIT', 'gaps': []}), \
             patch.object(t, 'do_close', side_effect=RuntimeError('partial')):
            r = m.run(True, True)['ZEC']
            self.assertEqual(r['action'], 'NEEDS_ATTENTION')
            self.assertEqual(r['position_after'], 'closing')


def fixture():
    now = int(time.time()*1000)
    p = {'live': True, 'spot': 'ZEC/USDT', 'perp': 'ZEC/USDT:USDT', 'size': 1,
         'base': 0, 'contracts': 0, 'phase': 'closed', 'opened_at': (now-10800000)/1000,
         'closed_at': (now-7200000)/1000, 'orders': []}
    fills, bills = [], []
    for i, (leg, side, price) in enumerate([('base','buy',100),('contracts','sell',101),
                                          ('contracts','buy',102),('base','sell',103)]):
        oid = str(i)
        p['orders'].append({'leg':leg,'side':side, 'result':{'id':oid,'filled':1,'fee':{'cost':.1,'currency':'USDT'}}})
        fills.append({'billId':str(10+i),'ordId':oid,'side':side,'fillSz':'1','fillPx':str(price),
                      'fee':'-0.1','feeCcy':'USDT','instId':'ZEC-USDT' if leg=='base' else 'ZEC-USDT-SWAP',
                      'ts':str(now-10000000+i*100)})
        bills.append({'billId':str(20+i),'type':'2','ordId':oid,'instId':fills[-1]['instId'], 'ts':fills[-1]['ts']})
    bills.append({'billId':'30','type':'8','ccy':'USDT','balChg':'.5','instId':'ZEC-USDT-SWAP','ts':str(now-9000000)})
    return p,fills,bills,now


class AccountingTests(unittest.TestCase):
    def test_base_currency_fee_not_double_counted(self):
        p,f,b,n = fixture()
        f[0]['feeCcy']='ZEC'; f[0]['fee']='-.01'
        p['orders'][0]['result']['fee']={'currency':'ZEC','cost':'.01'}
        f[3]['fillSz']='.99'; p['orders'][3]['result']['filled']='.99'
        self.assertEqual(a.summarize(p,f,b,n)['net_pnl_usdt'], '1.17')

    def test_read_failure_never_reports_profit(self):
        p,_,_,_=fixture()
        ex=Mock()
        ex.private_get_account_config.side_effect=TimeoutError('offline')
        self.assertIsNone(a.collect(ex,p,':memory:')['net_pnl_usdt'])

    def test_ioc_ccxt_request(self):
        ex=t.ccxt.okx()
        ex.set_markets([{'id':'ZEC-USDT','symbol':'ZEC/USDT','base':'ZEC','quote':'USDT',
                         'spot':True,'swap':False,'future':False,'option':False,'contract':False,
                         'type':'spot','linear':None,'inverse':None,
                         'precision':{'amount':.001,'price':.01}}])
        r=ex.create_order_request('ZEC/USDT','ioc','buy',2,100.2,{'tdMode':'cash','clOrdId':'onlytest'})
        self.assertEqual((r['ordType'],r['px'],r['sz']),('ioc','100.2','2'))

    def test_net_decimal(self):
        r = a.summarize(*fixture())
        self.assertEqual(r['net_pnl_usdt'], '2.1')
        self.assertEqual(r['status'], 'RECONCILED_AS_OF_QUERY')

    def test_missing_fills_null(self):
        p,f,b,n = fixture()
        self.assertIsNone(a.summarize(p,f[:-1],b,n)['net_pnl_usdt'])

    def test_missing_trade_bills_null(self):
        p,f,b,n = fixture()
        self.assertIn('TRADE_BILLS_MISSING',a.summarize(p,f,[],n)['reasons'])

    def test_foreign_fee_currency_null(self):
        p,f,b,n=fixture()
        f[0]['feeCcy']='OKB'
        self.assertIsNone(a.summarize(p,f,b,n)['net_pnl_usdt'])

    def test_open_and_archive_wait_null(self):
        p,f,b,n=fixture()
        p['phase']='open'
        self.assertIsNone(a.summarize(p,f,b,n)['net_pnl_usdt'])
        p['phase']='closed'; p['closed_at']=n/1000
        self.assertIsNone(a.summarize(p,f,b,n)['net_pnl_usdt'])

    def test_other_bill_null(self):
        p,f,b,n=fixture(); b.append({'type':'7'})
        self.assertIsNone(a.summarize(p,f,b,n)['net_pnl_usdt'])

    def test_pagination_requires_empty_and_cursor(self):
        fn=Mock(side_effect=[{'code':'0','data':[{'billId':'3'},{'billId':'2'}]},
                            {'code':'0','data':[{'billId':'1'}]}, {'code':'0','data':[]}])
        self.assertEqual(len(a.pages(fn,{})),3)
        self.assertEqual(fn.call_args_list[1].args[0]['after'],'2')

    def test_repeat_cursor_blocks(self):
        fn=Mock(return_value={'code':'0','data':[{'billId':'3'}]})
        with self.assertRaisesRegex(ValueError,'NOT_ADVANCING'):
            a.pages(fn,{})

    def test_archive_gap_no_api(self):
        p,f,b,n=fixture(); p['opened_at']=(n-100*a.DAY_MS)/1000
        ex=Mock()
        self.assertEqual(a.collect(ex,p,':memory:')['status'],'INVALID_DECLARED_GAP')
        ex.private_get_trade_fills_history.assert_not_called()

    def test_collect_dedup_and_conflict(self):
        p,f,b,n=fixture()
        ex=Mock()
        ex.markets={p['spot']:{'id':'ZEC-USDT'},p['perp']:{'id':'ZEC-USDT-SWAP'}}
        ex.private_get_account_config.return_value={'data':[{'uid':'test-only'}]}
        def method(rows, params):
            return {'code':'0','data': [] if 'after' in params else
                    [x for x in rows if 'instId' not in params or x['instId']==params['instId']]}
        ex.private_get_trade_fills_history.side_effect=lambda params:method(f,params)
        ex.private_get_account_bills_archive.side_effect=lambda params:method(b,params)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'a.sqlite'
            self.assertEqual(a.collect(ex,p,path)['net_pnl_usdt'],'2.1')
            self.assertEqual(a.collect(ex,p,path)['net_pnl_usdt'],'2.1')
            with sqlite3.connect(path) as db:
                self.assertEqual(db.execute('select count(*) from evidence').fetchone()[0],9)
            db.close()
            f[0]['fee']='-.2'
            self.assertEqual(a.collect(ex,p,path)['status'],'INVALID_DECLARED_GAP')


if __name__=='__main__':
    unittest.main()
