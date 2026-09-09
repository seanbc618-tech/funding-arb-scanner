"""F04 离线证据采集验收；SQLite 只使用临时目录。"""
import json
from pathlib import Path
import sqlite3
import sys
import tempfile
import unittest
from unittest.mock import Mock,patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import accounting as a

class Incremental(unittest.TestCase):
    def setUp(self):
        self.db=sqlite3.connect(':memory:');a.init_db(self.db);self.addCleanup(self.db.close)
        self.now=100*a.DAY_MS
        self.start=self.now-20*a.DAY_MS
        self.rows=[{'billId':'10','ts':str(self.now-a.DAY_MS),'type':'8','balChg':'1','ccy':'USDT'}]
        self.calls=[]
    def method(self,params):
        self.calls.append(params)
        rows=[r for r in self.rows if int(params['begin'])<=int(r['ts'])<=int(params['end']) and
              (not params.get('after') or int(r['billId'])<int(params['after']))]
        return {'code':'0','data':rows}
    def sync(self,now=None):
        now=self.now if now is None else now
        a.sync_stream(self.db,'acct','bills','ACCOUNT',self.method,{},self.start,now,now)
    def test_repeat_dedup(self):
        self.sync();self.sync()
        self.assertEqual(self.db.execute('SELECT count(*) FROM evidence').fetchone()[0],1)
    def test_incremental_overlap(self):
        self.sync();self.calls=[];self.sync(self.now+1000)
        self.assertEqual(int(self.calls[0]['begin']),self.now-a.OVERLAP_MS)
    def test_late_row_in_overlap(self):
        self.sync();self.rows.append(dict(self.rows[0],billId='11'))
        self.sync(self.now+1000)
        self.assertEqual(self.db.execute('SELECT count(*) FROM evidence').fetchone()[0],2)
    def test_resume_after_confirmed_page(self):
        first=self.method
        counter=[0]
        def fail(params):
            counter[0]+=1
            if counter[0]==2:raise TimeoutError()
            return first(params)
        with self.assertRaises(TimeoutError):
            a.sync_stream(self.db,'acct','bills','ACCOUNT',fail,{},self.start,self.now,self.now)
        self.assertEqual(self.db.execute('SELECT cursor,completed FROM collection_windows').fetchone(),('10',0))
        self.calls=[];self.sync()
        self.assertEqual(self.calls[0]['after'],'10')
        self.assertTrue(a.covered(self.db,'acct','bills','ACCOUNT',self.start,self.now))
    def test_resume_extends_to_current_end(self):
        m=Mock(side_effect=[{'code':'0','data':self.rows},TimeoutError()])
        with self.assertRaises(TimeoutError):a.sync_stream(self.db,'acct','bills','ACCOUNT',m,{},self.start,self.now,self.now)
        self.sync(self.now+1000)
        self.assertTrue(a.covered(self.db,'acct','bills','ACCOUNT',self.start,self.now+1000))
    def test_correction_preserves_old_and_records_conflict(self):
        self.sync();self.rows[0]['balChg']='2'
        with self.assertRaisesRegex(ValueError,'IMMUTABLE'):self.sync(self.now+1)
        raw=self.db.execute('SELECT raw FROM evidence').fetchone()[0]
        self.assertEqual(json.loads(raw)['balChg'],'1')
        self.assertEqual(self.db.execute('SELECT count(*) FROM evidence_conflicts').fetchone()[0],1)
    def test_old_coverage_remains_usable(self):
        self.sync()
        later=self.now+100*a.DAY_MS
        a.sync_stream(self.db,'acct','bills','ACCOUNT',Mock(side_effect=AssertionError('old API query')),{},self.start,self.now,later)
        self.assertTrue(a.covered(self.db,'acct','bills','ACCOUNT',self.start,self.now))
    def test_old_rows_without_coverage_not_complete(self):
        a.store(self.db,'acct','bills',self.rows)
        self.assertFalse(a.covered(self.db,'acct','bills','ACCOUNT',self.start,self.now))
    def test_hole_never_bridged(self):
        self.db.executemany('INSERT INTO collection_windows VALUES (?,?,?,?,?,NULL,1,?)',
            [('acct','bills','ACCOUNT',0,10,self.now),('acct','bills','ACCOUNT',20,30,self.now)])
        self.assertFalse(a.covered(self.db,'acct','bills','ACCOUNT',0,30))
    def test_source_gap_collects_recent_without_filling_old(self):
        self.start=0;self.sync()
        self.assertFalse(a.covered(self.db,'acct','bills','ACCOUNT',0,self.now))
        self.assertEqual(int(self.calls[0]['begin']),self.now-89*a.DAY_MS)
    def test_invalid_page_no_coverage(self):
        with self.assertRaises(ValueError):
            a.sync_stream(self.db,'acct','bills','ACCOUNT',lambda p:{'code':'0','data':None},{},self.start,self.now,self.now)
        self.assertFalse(a.covered(self.db,'acct','bills','ACCOUNT',self.start,self.now))
    def test_repeated_cursor_rejected(self):
        with self.assertRaisesRegex(ValueError,'NOT_ADVANCING'):
            a.sync_stream(self.db,'acct','bills','ACCOUNT',lambda p:{'code':'0','data':self.rows},{},self.start,self.now,self.now)
    def test_account_and_scope_isolation(self):
        self.sync()
        self.assertFalse(a.covered(self.db,'other','bills','ACCOUNT',self.start,self.now))
        self.assertFalse(a.covered(self.db,'acct','fills','ALL:SPOT',self.start,self.now))
    def test_outside_window_rejected(self):
        self.rows[0]['ts']='0'
        with self.assertRaisesRegex(ValueError,'OUTSIDE'):
            a.sync_stream(self.db,'acct','bills','ACCOUNT',lambda p:{'code':'0','data':self.rows},{},self.start,self.now,self.now)
    def test_raw_account_collection_no_strategy_profit(self):
        ex=Mock();ex.private_get_account_config.return_value={'data':[{'uid':'offline'}]}
        ex.private_get_trade_fills_history.return_value={'code':'0','data':[]}
        ex.private_get_account_bills_archive.return_value={'code':'0','data':[]}
        with tempfile.TemporaryDirectory() as tmp,patch.object(a.time,'time',return_value=self.now/1000):
            r=a.collect_account(ex,Path(tmp)/'e.sqlite',self.start)
        self.assertEqual(r['status'],'EVIDENCE_COLLECTED');self.assertIsNone(r['net_pnl_usdt'])
    def test_conflict_stays_blocking_after_retry(self):
        self.sync();self.rows[0]['balChg']='2'
        with self.assertRaises(ValueError):self.sync(self.now+1)
        self.rows[0]['balChg']='1';self.sync(self.now+1)
        self.assertEqual(self.db.execute('SELECT count(*) FROM evidence_conflicts').fetchone()[0],1)

    def test_collect_old_complete_local_windows_no_history_api(self):
        import hashlib
        ex=Mock();ex.private_get_account_config.return_value={'data':[{'uid':'offline'}]}
        ex.markets={'BTC/USDT':{'id':'BTC-USDT'},'BTC/USDT:USDT':{'id':'BTC-USDT-SWAP'}}
        ex.private_get_trade_fills_history.side_effect=AssertionError('expired API must not be queried')
        ex.private_get_account_bills_archive.side_effect=AssertionError('expired API must not be queried')
        p={'live':True,'opened_at':self.start/1000,'closed_at':self.now/1000,
           'spot':'BTC/USDT','perp':'BTC/USDT:USDT','base':0,'contracts':0,'size':1,'phase':'closed','orders':[]}
        account=hashlib.sha256(b'offline').hexdigest()
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'e.sqlite'
            with sqlite3.connect(path) as db:
                a.init_db(db)
                for kind,scope in [('fills','BTC-USDT'),('fills','BTC-USDT-SWAP'),('bills','ACCOUNT')]:
                    db.execute('INSERT INTO collection_windows VALUES (?,?,?,?,?,NULL,1,?)',
                               (account,kind,scope,self.start,self.now,self.now))
            with patch.object(a.time,'time',return_value=(self.now+100*a.DAY_MS)/1000):
                r=a.collect(ex,p,path)
        self.assertEqual(r['status'],'RECONCILIATION_REQUIRED')
        self.assertIn('NO_CONFIRMED_ORDERS',r['reasons'])
        self.assertNotIn('SOURCE_ARCHIVE_WINDOW_EXCEEDED',str(r['reasons']))
        ex.private_get_trade_fills_history.assert_not_called()
        ex.private_get_account_bills_archive.assert_not_called()

    def test_sync_evidence_persistent_conflict_blocks_report(self):
        import hashlib
        account=hashlib.sha256(b'offline').hexdigest()
        self.db.execute('INSERT INTO evidence_conflicts VALUES (?,?,?,?,?,?)',
                        (account,'bills','1','{}','{"corrected":true}',self.now))
        self.db.commit()
        ex=Mock();ex.private_get_account_config.return_value={'data':[{'uid':'offline'}]}
        ex.private_get_trade_fills_history.return_value={'code':'0','data':[]}
        ex.private_get_account_bills_archive.return_value={'code':'0','data':[]}
        _,gaps=a.sync_evidence(ex,self.db,self.start,self.now,self.now,[('BTC-USDT','SPOT')])
        self.assertIn('EVIDENCE_CONFLICT_REQUIRES_REVIEW',gaps)

if __name__=='__main__':unittest.main(verbosity=2)
