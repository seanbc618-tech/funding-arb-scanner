"""F07 复核：竞争条件与状态一致性；全为模拟交易所。"""
import copy
import sys
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import f07_execution as fixtures
import trade_a as t
import manage_a as m

class Review(unittest.TestCase):
    setUp = fixtures.ExecutionState.setUp

    def race(self, missing_id=False):
        t.do_open('ZEC',200);old=t.load()['ZEC']['trade_id']
        def replace_cycle(ex,p,private=True):
            self.assertEqual(p['trade_id'],old)
            if missing_id: p['trade_id']=None
            t.do_close('ZEC');t.do_open('ZEC',200)
            return {'action':'EXIT','gaps':[]}
        with patch.object(m,'check',side_effect=replace_cycle):
            result=m.run(True,True)
        self.assertEqual(t.load()['ZEC']['phase'],'open')
        self.assertNotEqual(t.load()['ZEC']['trade_id'],old)
        self.assertFalse(result['ZEC']['executed'])
        self.assertEqual(len(self.ex.calls),6)

    def test_manager_old_decision_cannot_close_new_cycle(self):
        self.race()

    def test_manager_missing_id_cannot_turn_into_manual_close(self):
        self.race(missing_id=True)

    def test_paused_without_pending_is_displayed_paused(self):
        t.do_open('ZEC',200);d=t.load();d['ZEC']['paused']=True;t.save(d)
        self.assertEqual(t.load()['ZEC']['execution_state'],'PAUSED')

    def test_new_cycle_cannot_reuse_historical_client_id(self):
        t.do_open('ZEC',200);t.do_close('ZEC');old=t.load()['ZEC']['orders'][0]['client_id']
        ids=[SimpleNamespace(hex=value) for value in ('b'*32,old,'c'*32)]
        with patch.object(t.uuid,'uuid4',side_effect=ids):
            with self.assertRaisesRegex(RuntimeError,'ORDER_INTENT'):
                t.do_open('ZEC',200)
        self.assertEqual(len(self.ex.calls),4)

if __name__=='__main__':unittest.main(verbosity=2)
