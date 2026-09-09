"""跨任务复核：只用临时数据库与模拟接口，禁止真实发送/交易。"""
import copy
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
from f05_portfolio import fixture, NOW
from f02_risk import Exchange
import accounting as a
import portfolio_a as v
import notify_events as n
import check_a as c
import trade_a


class Review(unittest.TestCase):
    def test_external_position_change_is_an_event(self):
        data={'mode':'account_read_only','status':'DATA_GAP','gaps':['ACCOUNT_POSITION_UNOWNED'],
              'local_positions':[],'account_positions':[{'symbol':'BTC/USDT:USDT','side':'long',
              'contracts':1,'markPrice':100,'liquidationPrice':50}]}
        before=n.event_view(data)
        data['account_positions'][0]['contracts']=2
        self.assertNotEqual(before,n.event_view(data))

    def test_price_noise_is_not_an_event(self):
        data={'mode':'account_read_only','status':'DATA_GAP','gaps':[], 'local_positions':[],
              'account_positions':[{'symbol':'BTC/USDT:USDT','side':'long','contracts':1,'markPrice':100,'liquidationPrice':50}]}
        before=n.event_view(data);data['account_positions'][0]['markPrice']=101
        self.assertEqual(before,n.event_view(data))

    def test_account_wide_evidence_reusable_without_rewriting_db(self):
        p,f,b=fixture(closed=True)
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'e.sqlite'
            with sqlite3.connect(path) as db:
                a.init_db(db);a.store(db,'a','fills',f);a.store(db,'a','bills',b)
                for k,s in [('fills','ALL:SPOT'),('fills','ALL:SWAP'),('bills','ACCOUNT')]:
                    db.execute('INSERT INTO collection_windows VALUES (?,?,?,?,?,NULL,1,?)',
                      ('a',k,s,int(p['opened_at']*1000),int(p['closed_at']*1000),NOW))
            before=path.read_bytes()
            x,y,g=v.read_evidence(path,'a',p,{'BTC/USDT':{'id':'BTC-USDT','type':'spot'},
                'BTC/USDT:USDT':{'id':'BTC-USDT-SWAP','type':'swap'}},NOW)
            self.assertEqual(g,[]);self.assertEqual((x,y),(f,b));self.assertEqual(path.read_bytes(),before)

    def test_wrong_instrument_does_not_reconcile_by_order_id(self):
        p,f,b=fixture(closed=True)
        # 模拟已有证据中订单号对应错误腿；订单必须同时匹配 symbol/instId。
        for o in p['orders']:o['symbol']=p['spot'] if o['leg']=='base' else p['perp']
        f[0]['instId']='BTC-USDT-SWAP'
        with self.assertRaisesRegex(ValueError,'FILL_INSTRUMENT_MISMATCH'):
            a.summarize(p,f,b,NOW,{'base':'BTC-USDT','contracts':'BTC-USDT-SWAP'})

    def report(self, *, slow=False, no_positions=False):
        p,f,b=fixture(closed=no_positions)
        ex=Mock();ex.private_get_account_config.return_value={'data':[{'uid':'offline'}]}
        ex.fetch_balance.return_value={'info':{'data':[{'totalEq':'1000'}]}}
        if no_positions:ex.fetch_positions.side_effect=TimeoutError()
        else:ex.fetch_positions.return_value=[]
        item={'gaps':[], 'quotes':{'base':{'book_timestamp':NOW}},
              'estimated_net_pnl_usdt':'1','estimated_net_after_exit_usdt':'2','exit_total_cost_usdt':'3',
              'unrealized_pnl_usdt':'4','exit_fee_estimate_usdt':'5','exit_spread_slippage_usdt':'6','spot_market_value_usdt':'7'}
        with patch.object(v.time,'time',return_value=NOW/1000), \
             patch.object(v.time,'monotonic',side_effect=[0,11 if slow else 0]), \
             patch.object(v,'read_evidence',return_value=(f,b,[])), \
             patch('trade_a.reconcile',return_value=(2,2)), \
             patch.object(v,'value_position',return_value=item):
            return v.report(ex,{'BTC':p},Path('/unused'))

    def test_slow_snapshot_clears_all_current_estimates(self):
        data=self.report(slow=True)
        self.assertIn('SNAPSHOT_TOO_SLOW',data['gaps'])
        for key in ('estimated_net_after_exit_usdt','exit_fee_estimate_usdt','exit_spread_slippage_usdt','spot_market_value_usdt'):
            self.assertIsNone(data['positions']['BTC'][key],key)

    def test_missing_position_query_never_claims_zero_margin(self):
        data=self.report(no_positions=True)
        self.assertIsNone(data['positions']['BTC']['perp_initial_margin_usd'])

    def test_hedge_mode_blocks_open_and_close(self):
        ex=Exchange();p=dict(spot='BTC/USDT',perp='BTC/USDT:USDT',base=2,contracts=2,
                         baseline=0,size=1,phase='open',live=True)
        with patch.object(ex,'private_get_account_config',return_value={'data':[{'acctLv':'2','posMode':'long_short_mode'}]}):
            for opening in (True,False):
                report=c.check(ex,p,opening=opening)
                self.assertEqual(report['action'],'BLOCK')
                self.assertEqual(report['execution']['status'],'BLOCKED')
            with self.assertRaisesRegex(RuntimeError,'net_mode'):trade_a.account(ex,p)


    def test_collect_rejects_wrong_instrument_end_to_end(self):
        p,f,b=fixture(closed=True);f[0]['instId']='BTC-USDT-SWAP'
        ex=Mock();ex.private_get_account_config.return_value={'data':[{'uid':'offline'}]}
        ex.markets={'BTC/USDT':{'id':'BTC-USDT'},'BTC/USDT:USDT':{'id':'BTC-USDT-SWAP'}}
        def page(rows,params):
            return {'code':'0','data':[row for row in rows if
                ('instId' not in params or row['instId']==params['instId']) and
                (not params.get('after') or int(row['billId'])<int(params['after']))]}
        ex.private_get_trade_fills_history.side_effect=lambda params:page(f,params)
        ex.private_get_account_bills_archive.side_effect=lambda params:page(b,params)
        with tempfile.TemporaryDirectory() as tmp,patch.object(a.time,'time',return_value=NOW/1000):
            report=a.collect(ex,p,Path(tmp)/'e.sqlite')
        self.assertIsNone(report['net_pnl_usdt'])
        self.assertIn('FILL_INSTRUMENT_MISMATCH',str(report['reasons']))

    def test_broad_coverage_does_not_cross_account_type_or_hole(self):
        with sqlite3.connect(':memory:') as db:
            a.init_db(db)
            for left,right in [(0,10),(20,30)]:
                db.execute('INSERT INTO collection_windows VALUES (?,?,?,?,?,NULL,1,?)',
                           ('a','fills','ALL:SPOT',left,right,30))
            self.assertTrue(a.covered(db,'a','fills','BTC-USDT',0,10,'ALL:SPOT'))
            self.assertFalse(a.covered(db,'a','fills','BTC-USDT',0,30,'ALL:SPOT'))
            self.assertFalse(a.covered(db,'a','fills','BTC-USDT-SWAP',0,10,'ALL:SWAP'))
            self.assertFalse(a.covered(db,'other','fills','BTC-USDT',0,10,'ALL:SPOT'))

    def test_account_event_is_sanitized_and_bounded(self):
        data={'mode':'account_read_only','status':'DATA_GAP','gaps':list(n.CODES),'local_positions':[],
              'account_positions':[{'symbol':'secret@host/path','side':'private','contracts':'secret'}]*100}
        msg=n.message(n.event_view(data),1000)
        self.assertNotIn('secret',msg);self.assertNotIn('private',msg)
        self.assertLessEqual(len(msg.encode('utf-16-le'))//2,4096)

if __name__=='__main__':unittest.main(verbosity=2)
