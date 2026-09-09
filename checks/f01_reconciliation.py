"""F01 离线验收：python3 checks/f01_reconciliation.py；不访问账户。"""
import copy
import sys
import unittest
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import monitor_a as m

LOCAL = dict(coin='BTC', phase='open', spot='BTC/USDT', perp='BTC/USDT:USDT',
             base=0.02, contracts=2, baseline=0.1, pending=False)
POSITION = dict(symbol='BTC/USDT:USDT', side='short', contracts=2,
                markPrice=100, liquidationPrice=150)


class Reconciliation(unittest.TestCase):
    def check_case(self, expected, local=None, positions=None, balances=None):
        local = [dict(LOCAL)] if local is None else local
        positions = [dict(POSITION)] if positions is None else positions
        balances = {'BTC': .12} if balances is None else balances
        before = copy.deepcopy((local, positions, balances))
        result = m.compare(local, positions, balances)
        self.assertEqual(result, expected)
        self.assertEqual((local, positions, balances), before)

    def test_healthy(self):
        self.check_case([])

    def test_wrong_side(self):
        self.check_case(['POSITION_SIDE_MISMATCH:BTC'], positions=[dict(POSITION, side='long')])

    def test_missing_perp(self):
        self.check_case(['LOCAL_POSITION_NOT_FOUND:BTC'], positions=[])

    def test_quantity(self):
        self.check_case(['POSITION_CONTRACTS_MISMATCH:BTC'], positions=[dict(POSITION, contracts=1)])

    def test_closed(self):
        self.check_case([], local=[dict(LOCAL, phase='closed', base=0, contracts=0)], positions=[], balances={})

    def test_closed_does_not_own_new_position(self):
        self.check_case(['ACCOUNT_POSITION_UNOWNED:BTC/USDT:USDT'],
                        local=[dict(LOCAL, phase='closed', base=0, contracts=0)], balances={})

    def test_missing_spot(self):
        self.check_case(['SPOT_BALANCE_MISMATCH:BTC'], balances={'BTC': .1})

    def test_unknown_spot(self):
        self.check_case(['SPOT_BALANCE_UNKNOWN:BTC'], balances={})

    def test_unknown_baseline(self):
        self.check_case(['SPOT_OWNERSHIP_UNKNOWN:BTC'], local=[dict(LOCAL, baseline=None)])

    def test_unknown_contracts(self):
        self.check_case(['POSITION_COMPARE_INVALID:BTC'], positions=[dict(POSITION, contracts=None)])

    def test_pending(self):
        self.check_case(['LOCAL_PENDING_UNKNOWN:BTC'], local=[dict(LOCAL, pending=True)])

    def test_duplicate_account_rows(self):
        self.check_case(['ACCOUNT_POSITION_AMBIGUOUS:BTC/USDT:USDT',
                         'POSITION_SIDE_MISMATCH:BTC', 'POSITION_CONTRACTS_MISMATCH:BTC'],
                        positions=[POSITION, dict(POSITION, side='long')])

    def test_unowned_balance(self):
        self.check_case(['ACCOUNT_BALANCE_UNOWNED:ETH'], balances={'BTC': .12, 'ETH': 1, 'USDT': 500})

    def test_unknown_local(self):
        self.check_case(['LOCAL_QUANTITY_UNKNOWN:BTC', 'POSITION_COMPARE_INVALID:BTC'],
                        local=[dict(LOCAL, contracts=None)])

    def test_closed_residual(self):
        self.check_case(['LOCAL_CLOSED_WITH_REMAINDER:BTC'], local=[dict(LOCAL, phase='closed')])

    def test_duplicate_ownership(self):
        self.check_case(['LOCAL_POSITION_AMBIGUOUS:BTC/USDT:USDT', 'SPOT_OWNERSHIP_AMBIGUOUS:BTC'],
                        local=[LOCAL, dict(LOCAL)])

    def test_snapshot_unknown_not_dropped(self):
        class Exchange:
            def fetch_positions(self):
                return [dict(POSITION, contracts=None)]
            def fetch_balance(self):
                return {'USDT': dict(free=10, used=0, total=10), 'total': {'USDT': 10, 'BTC': 0},
                        'info': {'data': [{'mgnRatio': '3'}]}}
        account = m.account_snapshot(Exchange())
        self.assertIsNone(account['positions'][0]['contracts'])
        self.assertEqual(account['balance_totals']['BTC'], 0)
        self.assertIn('POSITION_CONTRACTS_UNKNOWN:BTC/USDT:USDT', account['gaps'])

    def test_report_closed_no_positions(self):
        closed = dict(LOCAL, phase='closed', base=0, contracts=0)
        account = dict(positions=[], balance={}, nonzero_balances={}, balance_totals={}, gaps=[])
        with patch.object(m, 'read_local', return_value=({'BTC': closed}, [closed])), \
             patch.object(m, 'account_snapshot', return_value=account), \
             patch.dict(m.os.environ, {k: 'offline-placeholder' for k in m.ACCOUNT_ENV}):
            report, _ = m.snapshot(True, object())
        self.assertEqual(report['status'], 'NO_POSITIONS')


if __name__ == '__main__':
    unittest.main(verbosity=2)
