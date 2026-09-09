"""F06 离线验收；隔离状态与模拟发送，不访问实盘。"""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from datetime import datetime
from unittest.mock import Mock, patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import report_watch as r
from accounting import init_db


class Reports(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(); self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name); self.state = self.root/'state.json'; self.db = self.root/'db.sqlite'
        self.now = datetime(2026,9,9,8,tzinfo=r.TZ).timestamp()
        self.services = {'MONITOR':'active','NOTIFIER':'active'}
        self.sender = Mock(return_value={'status':'CONFIRMED','message_id':321})
        self.provider = Mock(return_value={'portfolio':{'checked_at_ms':self.now*1000,'status':'DATA_GAP',
                       'account':{},'positions':{}}})
        self.refresh()
        p=patch.object(r,'notifier_command',return_value=['mock']);p.start();self.addCleanup(p.stop)

    def refresh(self):
        (self.root/'monitor_a_status.json').write_text(json.dumps({'checked_at':self.now,
            'mode':'account_read_only','status':'OK','gaps':[],'local_positions':[]}))
        (self.root/'notify_events_health.json').write_text(json.dumps({'checked_at':self.now,'result':{'status':'UNCHANGED','delivery':'CONFIRMED'}}))

    def tick(self,hours=1):
        return r.tick(self.root,self.state,self.db,self.services,hours,now=self.now,sender=self.sender,provider=self.provider)

    def test_initial_daily_combines_hourly(self):
        result=self.tick(); self.assertEqual(len(result['deliveries']),1)
        self.assertEqual(result['deliveries'][0]['slot'],'daily')
        self.assertIn('2026-09-08',self.sender.call_args.args[0])
        self.assertEqual(self.state.stat().st_mode & 0o777,0o600)

    def test_restart_same_period_no_repeat(self):
        self.tick();self.tick();self.sender.assert_called_once();self.provider.assert_called_once()

    def test_next_hour_only_summary(self):
        self.tick();self.now+=3600;self.refresh()
        self.assertEqual(self.tick()['deliveries'][0]['slot'],'hourly')

    def test_four_hour_schedule(self):
        self.tick(4);self.now+=3600;self.refresh();self.tick(4);self.sender.assert_called_once()
        self.now+=3*3600;self.refresh();self.assertEqual(len(self.tick(4)['deliveries']),1)

    def test_daily_only_before_eight_no_send(self):
        self.now-=1;self.refresh();self.tick(0);self.sender.assert_not_called()

    def test_daily_only_once_and_next_day(self):
        self.tick(0);self.now+=3600;self.refresh();self.tick(0);self.sender.assert_called_once()
        self.now+=86400;self.refresh();self.assertEqual(len(self.tick(0)['deliveries']),1)

    def test_active_process_stalled_snapshot_alert(self):
        self.tick(0);self.now+=181
        h=self.root/'notify_events_health.json';h.write_text(json.dumps({'checked_at':self.now,'result':{'status':'UNCHANGED'}}))
        result=self.tick(0);self.assertEqual(result['health'],'MONITOR_STALE')
        self.assertEqual(result['deliveries'][0]['slot'],'health')

    def test_recovery_throttled_then_once(self):
        self.tick(0);self.now+=400;self.tick(0)
        self.now+=1;self.refresh();self.assertEqual(self.tick(0)['deliveries'],[])
        self.now+=300;self.refresh();self.assertEqual(len(self.tick(0)['deliveries']),1)
        self.assertIn('恢复',self.sender.call_args.args[0]);self.tick(0);self.assertEqual(self.sender.call_count,3)

    def test_dead_service_even_fresh_data(self):
        self.services['MONITOR']='inactive';self.assertIn('MONITOR_SERVICE_INACTIVE',self.tick(0)['health'])

    def test_notification_failed_heartbeat(self):
        (self.root/'notify_events_health.json').write_text(json.dumps({'checked_at':self.now,'result':{'status':'NOTIFICATION_CHECK_FAILED'}}))
        self.assertIn('NOTIFIER_CHECK_FAILED',self.tick(0)['health'])

    def test_notification_unknown_heartbeat(self):
        (self.root/'notify_events_health.json').write_text(json.dumps({'checked_at':self.now,'result':{'status':'UNCHANGED','delivery':'DELIVERY_UNKNOWN'}}))
        self.assertIn('NOTIFIER_DELIVERY_UNKNOWN',self.tick(0)['health'])

    def test_future_or_invalid_heartbeat(self):
        self.now-=10;self.assertIn('MONITOR_STALE',self.tick(0)['health'])
        (self.root/'monitor_a_status.json').write_text('{}')
        self.assertIn('MONITOR_HEARTBEAT_INVALID',self.tick(0)['health'])

    def test_unknown_delivery_not_retried(self):
        self.sender.return_value={'status':'DELIVERY_UNKNOWN'};self.tick();self.tick();self.sender.assert_called_once()

    def test_intent_durable_on_interruption(self):
        def send(*args):
            self.assertEqual(json.loads(self.state.read_text())['slots']['daily']['status'],'ATTEMPTING')
            raise KeyboardInterrupt()
        self.sender.side_effect=send
        with self.assertRaises(KeyboardInterrupt):self.tick()
        self.tick();self.sender.assert_called_once()

    def test_bad_state_no_send(self):
        self.state.write_text('{}')
        with self.assertRaises(ValueError):self.tick()
        self.sender.assert_not_called()

    def test_write_failure_no_send(self):
        with patch.object(r,'save',side_effect=OSError()):
            with self.assertRaises(OSError):self.tick()
        self.sender.assert_not_called()

    def test_clock_rollback_no_repeat(self):
        self.tick();self.now-=3600;self.refresh();self.tick();self.sender.assert_called_once()

    def test_stale_portfolio_never_current(self):
        msg=r.report_message(self.provider.return_value,self.now+91)
        self.assertIn('REPORT_UNAVAILABLE_OR_STALE',msg)
        self.assertNotIn('账户净资产 USD',msg)

    def test_message_raw_private_data_not_sent(self):
        data=self.provider.return_value;data['portfolio']['account']['equity_usd']='private-key'
        data['error']='private-error';msg=r.report_message(data,self.now,'2026-09-08')
        self.assertNotIn('private',msg);self.assertIn('昨日策略净收益：null',msg)

    def test_report_timeout_safe(self):
        with patch.object(r.subprocess,'run',side_effect=r.subprocess.TimeoutExpired('x',55)):
            self.assertEqual(r.query(self.root,self.db,None),{'error':'REPORT_UNAVAILABLE'})

    def test_daily_gap_does_not_create_db(self):
        self.assertIsNone(r.daily_ledger(self.db,'a','2026-09-08')['funding_usdt']);self.assertFalse(self.db.exists())

    def ledger(self, complete=True):
        start=int(datetime(2026,9,8,tzinfo=r.TZ).timestamp()*1000);end=start+86400000
        with sqlite3.connect(self.db) as db:
            init_db(db)
            for kind,scope in [('fills','ALL:SPOT'),('fills','ALL:SWAP'),('bills','ACCOUNT')]:
                db.execute('INSERT INTO collection_windows VALUES (?,?,?,?,?,NULL,?,?)',('a',kind,scope,start,end,int(complete),end))
            rows=[('bills','1',{'ts':str(start),'type':'8','ccy':'USDT','balChg':'2.5'}),
                  ('bills','2',{'ts':str(end),'type':'8','ccy':'USDT','balChg':'100'}),
                  ('fills','3',{'ts':str(start+1),'feeCcy':'USDT','fee':'-0.2'}),
                  ('fills','4',{'ts':str(start+2),'feeCcy':'USDT','fee':'0.01'})]
            for kind,id,row in rows:db.execute('INSERT INTO evidence VALUES (?,?,?,?)',('a',kind,id,json.dumps(row)))

    def test_daily_bounds_fees_and_readonly(self):
        self.ledger();before=self.db.read_bytes();result=r.daily_ledger(self.db,'a','2026-09-08')
        self.assertEqual(result['funding_usdt'],'2.5');self.assertEqual(result['fees_by_currency'],{'USDT':'0.19'})
        self.assertIsNone(result['strategy_net_pnl_usdt']);self.assertEqual(self.db.read_bytes(),before)

    def test_partial_coverage_null(self):
        self.ledger(False);self.assertIsNone(r.daily_ledger(self.db,'a','2026-09-08')['funding_usdt'])

    def test_account_isolation(self):
        self.ledger();self.assertIsNone(r.daily_ledger(self.db,'other','2026-09-08')['funding_usdt'])

    def test_conflict_blocks_daily(self):
        self.ledger()
        with sqlite3.connect(self.db) as db:db.execute('INSERT INTO evidence_conflicts VALUES (?,?,?,?,?,?)',('a','fills','1','{}','{}',0))
        self.assertEqual(r.daily_ledger(self.db,'a','2026-09-08')['gap'],'EVIDENCE_CONFLICT')


if __name__=='__main__':unittest.main(verbosity=2)
