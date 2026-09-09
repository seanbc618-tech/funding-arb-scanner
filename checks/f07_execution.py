"""F07 离线验收，模拟交易所，真实子进程锁；不连接账户、不发消息。"""
import copy
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import trade_a as t
from funding_arb_regression import Exchange


class ExecutionState(unittest.TestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory();self.addCleanup(self.tmp.cleanup)
        self.root=Path(self.tmp.name);self.ex=Exchange()
        for name,value in [('ROOT',self.root),('LIVE',True),('ACCOUNT_LOCK_ROOT',self.root/'locks')]:
            p=patch.object(t,name,value);p.start();self.addCleanup(p.stop)
        p=patch.object(t,'exchange',return_value=self.ex);p.start();self.addCleanup(p.stop)

    def test_cycle_id_targets_quantities_and_fees(self):
        t.do_open('ZEC',200);p=t.load()['ZEC']
        self.assertEqual(len(p['trade_id']),32);self.assertEqual(p['targets'],{'base':2,'contracts':2})
        self.assertEqual(p['execution_state'],'OPEN');self.assertEqual(p['exposure_base'],0)
        self.assertEqual(p['confirmed_remaining'],{'base':2,'contracts':2})
        self.assertEqual(p['fees_by_currency'],{'USDT':0})
        self.assertTrue(all(o['exchange_order_id'] and o['trade_id']==p['trade_id'] and o['state']=='TERMINAL' for o in p['orders']))
        ident=p['trade_id'];t.do_close('ZEC');t.do_open('ZEC',200)
        after=t.load()['ZEC'];self.assertNotEqual(after['trade_id'],ident)
        self.assertEqual(after['previous']['trade_id'],ident)
        self.assertEqual(after['previous']['execution_state'],'CLOSED')
        self.assertEqual(t.state_path().stat().st_mode & 0o777,0o600)

    def test_pending_other_coin_blocks_before_plan_or_order(self):
        self.ex.effects=[{'unknown':True}]
        with self.assertRaises(KeyError):t.do_open('ZEC',200)
        before=t.state_path().read_bytes()
        with patch.object(t,'plan',side_effect=AssertionError('must not plan')):
            with self.assertRaisesRegex(RuntimeError,'PENDING_ORDER_RECOVER_FIRST'):t.do_open('BTC',200)
        self.assertEqual(t.state_path().read_bytes(),before);self.assertEqual(len(self.ex.calls),1)
        self.assertEqual(t.load()['ZEC']['execution_state'],'PENDING_UNKNOWN')

    def test_known_open_order_is_not_applied_until_terminal(self):
        self.ex.effects=[{'status':'open'}]
        with self.assertRaises(RuntimeError):t.do_open('ZEC',200)
        p=t.load()['ZEC'];self.assertEqual(p['execution_state'],'PENDING_KNOWN')
        self.assertEqual(p['base'],0);self.assertEqual(p['pending']['observed_filled'],2)
        self.ex.orders[p['pending']['client_id']]['status']='closed'
        t.do_recover('ZEC');t.do_recover('ZEC')
        self.assertEqual(t.load()['ZEC']['base'],2);self.assertEqual(len(self.ex.calls),1)
        self.assertEqual(t.load()['ZEC']['execution_state'],'RECOVERY_REQUIRED')
        with self.assertRaisesRegex(RuntimeError,'RECOVERY_REQUIRED'):t.do_open('BTC',200)

    def test_crash_after_accepted_before_ack_does_not_resubmit(self):
        create=self.ex.create_order
        def crash(*args):create(*args);raise KeyboardInterrupt()
        with patch.object(self.ex,'create_order',side_effect=crash):
            with self.assertRaises(KeyboardInterrupt):t.do_open('ZEC',200)
        self.assertEqual(t.load()['ZEC']['pending']['state'],'SUBMIT_INTENT')
        t.do_recover('ZEC');self.assertEqual(t.load()['ZEC']['base'],2)
        self.assertEqual(len(self.ex.calls),1)

    def test_disk_failure_after_terminal_can_recover_once(self):
        original=t.save;failed=[False]
        def save(d):
            p=d.get('ZEC',{})
            if p.get('orders') and not p.get('pending') and not failed[0]:
                failed[0]=True;raise OSError('disk')
            original(d)
        with patch.object(t,'save',side_effect=save):
            with self.assertRaises(OSError):t.do_open('ZEC',200)
        self.assertIn('pending',t.load()['ZEC'])
        t.do_recover('ZEC');t.do_recover('ZEC')
        self.assertEqual(t.load()['ZEC']['base'],2);self.assertEqual(len(self.ex.calls),1)

    def test_intent_write_failure_never_submits(self):
        original=t.save
        def save(d):
            if any(p.get('pending') for p in d.values()):raise OSError('disk')
            original(d)
        with patch.object(t,'save',side_effect=save):
            with self.assertRaises(OSError):t.do_open('ZEC',200)
        self.assertFalse(self.ex.calls)
        with self.assertRaisesRegex(RuntimeError,'RECOVERY_REQUIRED'):t.do_open('BTC',200)

    def test_account_id_change_blocks_saved_state(self):
        t.do_open('ZEC',200);before=t.state_path().read_bytes()
        with patch.object(self.ex,'private_get_account_config',return_value={'data':[{'uid':'other','acctLv':'2','posMode':'net_mode'}]}):
            with self.assertRaisesRegex(RuntimeError,'STATE_ACCOUNT_MISMATCH'):t.do_close('ZEC')
        self.assertEqual(t.state_path().read_bytes(),before)

    def test_unknown_uid_prevents_orders(self):
        with patch.object(self.ex,'private_get_account_config',return_value={'data':[{'acctLv':'2','posMode':'net_mode'}]}):
            with self.assertRaisesRegex(RuntimeError,'ACCOUNT_ID_UNKNOWN'):t.do_open('ZEC',200)
        self.assertFalse(self.ex.calls)

    def test_legacy_state_unchanged_and_readable(self):
        t.state_path().write_text(json.dumps({'ZEC':{'live':True,'phase':'open','base':1,'contracts':1}}))
        before=t.state_path().read_bytes()
        with self.assertRaisesRegex(RuntimeError,'LEGACY_STATE_REVIEW_REQUIRED'):t.do_close('ZEC')
        self.assertEqual(t.load()['ZEC']['base'],1);self.assertEqual(t.state_path().read_bytes(),before)

    def test_other_external_position_blocks_startup(self):
        with patch.object(self.ex,'fetch_positions',return_value=[{'symbol':'BTC/USDT:USDT','contracts':1}]):
            with self.assertRaisesRegex(RuntimeError,'ACCOUNT_POSITION_UNOWNED'):t.do_open('ZEC',200)
        self.assertFalse(self.ex.calls)

    def test_regular_and_algo_open_orders_block(self):
        for kind in (None,'conditional','oco','trigger','move_order_stop','iceberg','twap'):
            with self.subTest(kind=kind):
                def orders(sym=None,params=None):return [{}] if (params or {}).get('ordType')==kind else []
                with patch.object(self.ex,'fetch_open_orders',side_effect=orders):
                    with self.assertRaisesRegex(RuntimeError,'ACCOUNT_OPEN_ORDERS'):t.do_open('ZEC',200)
        self.assertFalse(self.ex.calls)

    def test_unknown_order_list_blocks(self):
        with patch.object(self.ex,'fetch_open_orders',return_value=None):
            with self.assertRaisesRegex(RuntimeError,'OPEN_ORDERS_UNKNOWN'):t.do_open('ZEC',200)
        self.assertFalse(self.ex.calls)

    def test_other_position_drift_blocks_new_coin(self):
        t.do_open('ZEC',200);self.ex.short=3
        with patch.object(t,'plan',side_effect=AssertionError('must not plan')):
            with self.assertRaisesRegex(RuntimeError,'账户不符'):t.do_open('BTC',200)
        self.assertEqual(len(self.ex.calls),2)

    def test_hedge_mode_blocks_new_intent(self):
        with patch.object(self.ex,'private_get_account_config',return_value={'data':[{'uid':'offline-test-account','acctLv':'2','posMode':'long_short_mode'}]}):
            with self.assertRaisesRegex(RuntimeError,'ACCOUNT_MODE_UNSUPPORTED'):t.do_open('ZEC',200)
        self.assertFalse(self.ex.calls);self.assertFalse(t.state_path().exists())

    def test_identity_mismatch_does_not_apply_fill(self):
        self.ex.effects=[{'status':'open'}]
        with self.assertRaises(RuntimeError):t.do_open('ZEC',200)
        pending=t.load()['ZEC']['pending'];self.ex.orders[pending['client_id']].update(status='closed',clientOrderId='other')
        before=t.state_path().read_bytes()
        with self.assertRaisesRegex(RuntimeError,'客户订单号不匹配'):t.do_recover('ZEC')
        self.assertEqual(t.state_path().read_bytes(),before)

    def test_completed_intent_cannot_be_replayed(self):
        t.do_open('ZEC',200);d=t.load();p=d['ZEC'];p['pending']=copy.deepcopy(p['orders'][0]);t.save(d)
        with self.assertRaisesRegex(RuntimeError,'DUPLICATE_ORDER_INTENT'):t.do_recover('ZEC')
        self.assertEqual(len(self.ex.calls),2)

    def test_low_level_order_requires_matching_execution_session(self):
        with self.assertRaisesRegex(RuntimeError,'EXECUTION_SESSION_REQUIRED'):t.order(self.ex,{}, {},'base','buy',1)
        with t.execution_session():
            with self.assertRaisesRegex(RuntimeError,'EXECUTION_CONTEXT_MISMATCH'):t.order(Exchange(),{}, {},'base','buy',1)
        self.assertFalse(self.ex.calls)

    def test_different_checkout_same_account_cannot_bind_new_state(self):
        with t.execution_session():pass
        other=self.root/'other';other.mkdir()
        with patch.object(t,'ROOT',other):
            with self.assertRaisesRegex(RuntimeError,'ACCOUNT_BOUND_TO_ANOTHER_STATE'):
                with t.execution_session():pass
        self.assertFalse((other/'positions_a_live.json').exists())

    def test_bad_account_binding_is_not_overwritten(self):
        with t.execution_session():pass
        file=next(t.ACCOUNT_LOCK_ROOT.glob('*.lock'));file.write_text('{broken')
        with self.assertRaises(ValueError):
            with t.execution_session():pass
        self.assertEqual(file.read_text(),'{broken')

    def test_real_process_account_lock_blocks_another_checkout(self):
        other=self.root/'child';other.mkdir()
        code='''import sys
from pathlib import Path
sys.path[:0]=[sys.argv[1],sys.argv[1]+'/checks']
import trade_a as t
from funding_arb_regression import Exchange
t.ROOT=Path(sys.argv[2]);t.LIVE=True;t.ACCOUNT_LOCK_ROOT=Path(sys.argv[3]);t.exchange=lambda:Exchange()
with t.execution_session():
 print('LOCKED',flush=True)
 sys.stdin.readline()
'''
        child=subprocess.Popen([sys.executable,'-B','-c',code,str(Path(__file__).resolve().parents[1]),str(other),str(t.ACCOUNT_LOCK_ROOT)],stdin=subprocess.PIPE,stdout=subprocess.PIPE,stderr=subprocess.PIPE,text=True)
        try:
            self.assertEqual(child.stdout.readline().strip(),'LOCKED')
            with self.assertRaises(BlockingIOError):
                with t.execution_session():pass
            child.communicate('\n',timeout=10);self.assertEqual(child.returncode,0)
            with self.assertRaisesRegex(RuntimeError,'ACCOUNT_BOUND_TO_ANOTHER_STATE'):
                with t.execution_session():pass
        finally:
            if child.poll() is None:child.kill();child.communicate()

    def test_startup_reports_paused_without_mutating_trade_state(self):
        self.ex.effects=[{'unknown':True}]
        with self.assertRaises(KeyError):t.do_open('ZEC',200)
        before=t.state_path().read_bytes();result=t.do_startup()
        self.assertEqual(result['status'],'PAUSED');self.assertEqual(t.state_path().read_bytes(),before)


    def test_duplicate_generated_client_id_blocked_before_second_send(self):
        from types import SimpleNamespace
        with patch.object(t.uuid,'uuid4',return_value=SimpleNamespace(hex='a'*32)):
            with self.assertRaisesRegex(RuntimeError,'DUPLICATE_ORDER_INTENT'):t.do_open('ZEC',200)
        self.assertEqual(len(self.ex.calls),1)
        self.assertEqual(t.load()['ZEC']['base'],2)

if __name__=='__main__':unittest.main(verbosity=2)
