"""F03 离线验收；全部发送接口为 mock。"""
import copy
import json
from pathlib import Path
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import notify_events as n
import notify_run


class Events(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.snapshot = Path(self.tmp.name) / 'snapshot.json'
        self.state = Path(self.tmp.name) / 'state.json'
        self.report = {'mode':'account_read_only','status':'CONFIG_MISSING','checked_at':1000,
                       'local_positions':[], 'gaps':['MISSING_CREDENTIALS:private-text']}
        self.sender = Mock(return_value={'status':'CONFIRMED','message_id':123})
        patcher = patch.object(n,'notifier_command',return_value=['mock'])
        patcher.start(); self.addCleanup(patcher.stop)

    def tick(self, now=1000):
        self.snapshot.write_text(json.dumps(self.report))
        before = self.snapshot.read_bytes()
        r = n.tick(self.snapshot,self.state,now=now,sender=self.sender)
        self.assertEqual(self.snapshot.read_bytes(),before)
        return r

    def test_initial_event(self):
        self.assertEqual(self.tick()['status'],'CONFIRMED')
        self.assertEqual(self.state.stat().st_mode & 0o777,0o600)

    def test_same_event_restart_no_repeat(self):
        self.tick(); self.report['checked_at']=1400
        self.assertEqual(self.tick(1400)['status'],'UNCHANGED')
        self.sender.assert_called_once()

    def test_only_clock_change_no_repeat(self):
        self.tick();self.report['checked_at']=1010
        self.assertEqual(self.tick(1010)['status'],'UNCHANGED')

    def test_change_throttled_then_latest(self):
        self.tick(); self.report.update(status='ERROR',gaps=['API_ERROR:private'])
        self.assertEqual(self.tick(1010)['status'],'THROTTLED')
        self.report.update(status='OK',gaps=[],checked_at=1400)
        self.assertEqual(self.tick(1400)['status'],'CONFIRMED')
        self.assertIn('状态：OK',self.sender.call_args.args[0])
        self.assertEqual(self.sender.call_count,2)

    def test_unknown_delivery_no_retry(self):
        self.sender.return_value={'status':'DELIVERY_UNKNOWN'}
        self.assertEqual(self.tick()['status'],'DELIVERY_UNKNOWN')
        self.report['checked_at']=1500
        self.assertEqual(self.tick(1500)['status'],'UNCHANGED')
        self.sender.assert_called_once()

    def test_sender_exception_no_retry(self):
        self.sender.side_effect=TimeoutError('private-text')
        self.assertEqual(self.tick()['status'],'DELIVERY_UNKNOWN')
        self.assertNotIn('private-text',self.state.read_text())
        self.assertEqual(self.tick()['status'],'UNCHANGED')

    def test_intent_durable_before_send(self):
        def send(*args):
            self.assertEqual(json.loads(self.state.read_text())['status'],'ATTEMPTING')
            raise KeyboardInterrupt()
        self.sender.side_effect=send
        with self.assertRaises(KeyboardInterrupt): self.tick()
        self.assertEqual(self.tick()['status'],'UNCHANGED')
        self.sender.assert_called_once()

    def test_state_write_failure_prevents_send(self):
        with patch.object(n,'save',side_effect=OSError('disk')):
            with self.assertRaises(OSError): self.tick()
        self.sender.assert_not_called()

    def test_bad_state_prevents_send(self):
        self.state.write_text('{}')
        with self.assertRaises(ValueError): self.tick()
        self.sender.assert_not_called()

    def test_stale_and_future_snapshot(self):
        for now in (999,1181):
            self.assertEqual(self.tick(now)['status'],'SNAPSHOT_STALE')
        self.sender.assert_not_called()

    def test_local_only_never_sends(self):
        self.report['mode']='local_only'
        with self.assertRaises(ValueError): self.tick()
        self.sender.assert_not_called()

    def test_no_sensitive_raw_fields(self):
        self.report.update(gaps=['API_ERROR:secret-token https://host/user','random-secret'],
                           password='private-password',account_positions=[],
                           local_positions=[dict(coin='ssh@1.2.3.4',phase='private-phase',
                                                 base=None,contracts=0,pending=False)])
        self.tick(); msg=self.sender.call_args.args[0]
        for token in ('secret-token','host/user','password','private-phase','ssh@1.2.3.4'):
            self.assertNotIn(token,msg)
        self.assertIn('OTHER_DATA_GAP',msg)

    def test_pending_and_local_phase_event(self):
        self.tick();self.report.update(checked_at=1400,local_positions=[
            dict(coin='BTC',phase='opening',base=.1,contracts=0,pending=True)])
        self.assertEqual(self.tick(1400)['status'],'CONFIRMED')
        self.assertIn('未确认订单 True',self.sender.call_args.args[0])

    def test_gap_raw_detail_change_no_repeat(self):
        self.tick();self.report['gaps']=['MISSING_CREDENTIALS:different-secret']
        self.assertEqual(self.tick()['status'],'UNCHANGED')

    def test_risk_threshold_event_no_price_noise(self):
        self.report.update(balance={'margin_ratio':4},account_positions=[
            dict(side='short',markPrice=100,liquidationPrice=150)])
        self.tick();self.report.update(checked_at=1400,balance={'margin_ratio':2})
        self.assertEqual(self.tick(1400)['status'],'CONFIRMED')
        self.assertIn('MARGIN_RATIO_LOW',self.sender.call_args.args[0])
        self.report['balance']['margin_ratio']=1.9
        self.assertEqual(self.tick(1400)['status'],'UNCHANGED')

    def test_configuration_failure_no_intent(self):
        with patch.object(n,'notifier_command',side_effect=ValueError('config')):
            with self.assertRaises(ValueError): self.tick()
        self.assertFalse(self.state.exists()); self.sender.assert_not_called()

    def test_message_size_bound(self):
        self.report['local_positions']=[dict(coin='BTC',phase='open',base=1,contracts=1) for _ in range(100)]
        self.report['gaps']=list(n.CODES)
        self.tick()
        self.assertLessEqual(len(self.sender.call_args.args[0].encode('utf-16-le'))//2,4096)

    def test_send_timeout_once(self):
        with patch.object(notify_run.subprocess,'run',side_effect=subprocess.TimeoutExpired('mock',35)) as run:
            self.assertEqual(notify_run.send_message('test',['mock'])['status'],'DELIVERY_UNKNOWN')
        run.assert_called_once()

    def test_send_receipt_allowlist(self):
        with patch.object(notify_run.subprocess,'run',return_value=Mock(returncode=0,stdout=json.dumps(
                {'ok':True,'message_id':1,'chat_id':'private'}))):
            self.assertEqual(notify_run.send_message('test',['mock']),{'status':'CONFIRMED','message_id':1})


if __name__=='__main__':
    unittest.main(verbosity=2)
