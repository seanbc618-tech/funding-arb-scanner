"""F02 离线验收：python3 checks/f02_risk.py；不连接交易所、不产生真实订单。"""
import copy
import json
import math
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
import check_a as c
import manage_a
import trade_a


class Exchange:
    def __init__(self):
        self.balance = {'BTC': {'total': 2}, 'USDT': {'free': 10000},
                        'info': {'data': [{'mgnRatio': '10'}]}}
        self.positions = [{'symbol': 'BTC/USDT:USDT', 'contracts': 2, 'side': 'short',
                           'marginMode': 'cross', 'markPrice': 100, 'liquidationPrice': 150}]
        self.books_seen = []

    def fetch_balance(self):
        return copy.deepcopy(self.balance)

    def fetch_positions(self, symbols=None):
        return copy.deepcopy(self.positions)

    def private_get_account_config(self):
        return {'data': [{'acctLv': '2', 'posMode': 'net_mode'}]}

    def fetch_open_orders(self, symbol):
        return []

    def fetch_order_book(self, symbol, limit=50):
        self.books_seen.append(symbol)
        return {'timestamp': int(time.time() * 1000),
                'asks': [[100.01, 100]], 'bids': [[100, 100]]}

    def price_to_precision(self, symbol, price):
        return str(math.floor(price * 100) / 100)

    def fetch_funding_rate(self, symbol):
        return {'fundingRate': .0001, 'fundingTimestamp': time.time() * 1000 + 3600000}


class RiskTests(unittest.TestCase):
    def setUp(self):
        self.ex = Exchange()
        self.p = dict(spot='BTC/USDT', perp='BTC/USDT:USDT', base=2, contracts=2,
                      baseline=0, size=1, phase='open', live=True)

    def run_check(self, **kwargs):
        before = copy.deepcopy(self.p)
        report = c.check(self.ex, self.p, **kwargs)
        self.assertEqual(before, self.p)
        json.dumps(report, allow_nan=False)
        return report

    def low_risk(self):
        self.ex.balance['info']['data'][0]['mgnRatio'] = '2'
        self.ex.positions[0]['liquidationPrice'] = 105

    def assert_known_risk_blocked(self, report):
        self.assertEqual(report['action'], 'BLOCK')
        self.assertEqual(report['risk']['status'], 'EXIT_REQUIRED')
        self.assertEqual(report['execution']['status'], 'BLOCKED')
        self.assertEqual(report['data_status'], 'INCOMPLETE')
        self.assertIn('MARGIN_RATIO_LOW', report['risk']['reasons'])
        self.assertIn('LIQUIDATION_DISTANCE_LOW', report['risk']['reasons'])
        self.assertEqual(report['metrics']['margin_ratio'], 2)
        self.assertAlmostEqual(report['metrics']['liquidation_distance'], .05)

    def test_healthy_hold(self):
        r = self.run_check()
        self.assertEqual((r['action'], r['risk']['status'], r['execution']['status'], r['data_status']),
                         ('HOLD', 'CLEAR', 'READY', 'COMPLETE'))

    def test_books_fail_risks_survive(self):
        self.low_risk()
        with patch.object(self.ex, 'fetch_order_book', side_effect=TimeoutError('offline')) as fetch:
            self.assert_known_risk_blocked(self.run_check())
        self.assertEqual(fetch.call_count, 2)

    def test_depth_fails_risks_survive_and_other_leg_collected(self):
        self.low_risk()
        original = self.ex.fetch_order_book
        def fetch(symbol, limit=50):
            book = original(symbol, limit)
            if symbol == self.p['spot']:
                book['bids'] = [[100, .001]]
            return book
        with patch.object(self.ex, 'fetch_order_book', side_effect=fetch):
            r = self.run_check()
        self.assert_known_risk_blocked(r)
        self.assertIsNotNone(r['metrics']['perp_execution'])
        self.assertTrue(any('INSUFFICIENT_DEPTH' in g for g in r['gaps']))

    def test_funding_failure_does_not_hide_private_risk(self):
        self.low_risk()
        with patch.object(self.ex, 'fetch_funding_rate', side_effect=TimeoutError('offline')):
            self.assert_known_risk_blocked(self.run_check())

    def test_margin_unknown_does_not_hide_liquidation(self):
        self.low_risk()
        self.ex.balance['info']['data'][0]['mgnRatio'] = None
        r = self.run_check()
        self.assertEqual(r['risk']['status'], 'EXIT_REQUIRED')
        self.assertIn('LIQUIDATION_DISTANCE_LOW', r['reasons'])
        self.assertEqual(r['action'], 'BLOCK')

    def test_liquidation_unknown_does_not_hide_margin(self):
        self.low_risk()
        self.ex.positions[0]['liquidationPrice'] = None
        r = self.run_check()
        self.assertEqual(r['risk']['status'], 'EXIT_REQUIRED')
        self.assertIn('MARGIN_RATIO_LOW', r['reasons'])
        self.assertEqual(r['action'], 'BLOCK')

    def test_balance_failure_does_not_hide_liquidation(self):
        self.low_risk()
        with patch.object(self.ex, 'fetch_balance', side_effect=TimeoutError('offline')):
            r = self.run_check()
        self.assertIn('LIQUIDATION_DISTANCE_LOW', r['reasons'])
        self.assertEqual(r['action'], 'BLOCK')

    def test_positions_failure_does_not_hide_margin(self):
        self.low_risk()
        with patch.object(self.ex, 'fetch_positions', side_effect=TimeoutError('offline')):
            r = self.run_check()
        self.assertIn('MARGIN_RATIO_LOW', r['reasons'])
        self.assertEqual(r['action'], 'BLOCK')

    def test_bad_position_row_does_not_hide_other_risk(self):
        self.low_risk()
        self.ex.positions.insert(0, None)
        self.assert_known_risk_blocked(self.run_check())

    def test_all_position_rows_invalid_still_checks_margin(self):
        self.low_risk()
        self.ex.positions = [None]
        r = self.run_check()
        self.assertIn('MARGIN_RATIO_LOW', r['reasons'])
        self.assertEqual(r['action'], 'BLOCK')

    def test_unknown_contracts_still_checks_prices(self):
        self.low_risk()
        self.ex.positions[0]['contracts'] = None
        self.assert_known_risk_blocked(self.run_check())

    def test_reconcile_failure_does_not_hide_risk(self):
        self.low_risk()
        self.ex.balance['BTC']['total'] = 3
        self.assert_known_risk_blocked(self.run_check())

    def test_pending_still_checks_risk(self):
        self.low_risk()
        self.p['pending'] = {'client_id': 'offline'}
        self.assert_known_risk_blocked(self.run_check())

    def test_wrong_direction_and_long_liquidation_formula(self):
        self.ex.positions[0].update(side='long', liquidationPrice=95)
        r = self.run_check()
        self.assertEqual(r['action'], 'BLOCK')
        self.assertIn('POSITION_SIDE_MISMATCH', r['risk']['reasons'])
        self.assertIn('LIQUIDATION_DISTANCE_LOW', r['risk']['reasons'])
        self.assertAlmostEqual(r['metrics']['liquidation_distance'], .05)

    def test_unknown_direction_has_no_invented_distance(self):
        self.ex.positions[0]['side'] = None
        r = self.run_check()
        self.assertEqual(r['risk']['status'], 'UNKNOWN')
        self.assertNotIn('liquidation_distance', r['metrics'])
        self.assertEqual(r['action'], 'BLOCK')

    def test_duplicate_rows_never_pass(self):
        self.ex.positions = [dict(self.ex.positions[0], contracts=1)] * 2
        r = self.run_check()
        self.assertEqual(r['action'], 'BLOCK')
        self.assertTrue(any('DUPLICATE_TARGET' in g for g in r['gaps']))

    def test_unknown_local_quantity_other_book_and_risk_survive(self):
        self.low_risk()
        self.p['base'] = None
        r = self.run_check()
        self.assert_known_risk_blocked(r)
        self.assertIsNotNone(r['metrics']['perp_execution'])

    def test_negative_funding_exit_without_limits(self):
        with patch.object(self.ex, 'fetch_funding_rate', return_value={
                'fundingRate': -.001, 'fundingTimestamp': time.time() * 1000 + 60000}):
            r = self.run_check()
        self.assertEqual((r['action'], r['risk']['status'], r['execution']['status']),
                         ('EXIT', 'EXIT_REQUIRED', 'READY'))

    def test_bad_funding_clock_still_checks_margin(self):
        self.low_risk()
        with patch.object(self.ex, 'fetch_funding_rate', return_value={
                'fundingRate': .001, 'fundingTimestamp': 0}):
            self.assert_known_risk_blocked(self.run_check())

    def test_book_failure_alone_does_not_claim_unknown_account_risk(self):
        with patch.object(self.ex, 'fetch_order_book', side_effect=TimeoutError('offline')):
            r = self.run_check()
        self.assertEqual(r['risk']['status'], 'CLEAR')
        self.assertEqual(r['action'], 'BLOCK')

    def test_slow_snapshot_preserves_risk_and_blocks(self):
        self.low_risk()
        with patch.object(c.time, 'monotonic', side_effect=[0, 11]):
            r = self.run_check()
        self.assert_known_risk_blocked(r)
        self.assertTrue(any('SNAPSHOT_TOO_SLOW' in g for g in r['gaps']))

    def test_book_ages_while_second_leg_fetches(self):
        clock = [1000.0]
        original = self.ex.fetch_order_book
        def fetch(symbol, limit=50):
            if symbol == self.p['perp']:
                clock[0] += 6
            return original(symbol, limit)
        with patch.object(c.time, 'time', side_effect=lambda: clock[0]), \
             patch.object(self.ex, 'fetch_order_book', side_effect=fetch):
            r = self.run_check()
        self.assertEqual(r['action'], 'BLOCK')
        self.assertTrue(any('STALE_OR_FUTURE_AT_COMPLETION' in g for g in r['gaps']))

    def test_public_only_never_queries_account(self):
        with patch.object(self.ex, 'fetch_positions', side_effect=AssertionError('private call')) as positions, \
             patch.object(self.ex, 'fetch_balance', side_effect=AssertionError('private call')) as balance:
            r = self.run_check(private=False)
        positions.assert_not_called()
        balance.assert_not_called()
        self.assertEqual(r['risk']['scope'], 'local_and_public')
        self.assertEqual(r['action'], 'HOLD')

    def test_closed_is_only_local_fact(self):
        self.p.update(phase='closed', base=0, contracts=0)
        with patch.object(self.ex, 'fetch_positions') as positions:
            r = self.run_check()
        positions.assert_not_called()
        self.assertEqual((r['action'], r['risk']['scope']), ('CLOSED', 'local_closed_record'))

    def test_closed_with_residual_does_not_skip_risk(self):
        self.low_risk()
        self.p['phase'] = 'closed'
        self.assert_known_risk_blocked(self.run_check())

    def test_entry_capital_still_required(self):
        self.p['phase'] = 'opening'
        self.ex.positions = []
        self.ex.balance['USDT']['free'] = 100
        r = self.run_check(opening=True)
        self.assertEqual(r['action'], 'BLOCK')
        self.assertIn('INSUFFICIENT_FREE_USDT', r['execution']['limitations'])
        self.assertGreater(r['metrics']['required_usdt'], 400)

    def test_entry_passes_when_ready(self):
        self.p['phase'] = 'opening'
        self.ex.positions = []
        self.assertEqual(self.run_check(opening=True)['action'], 'PASS')

    def test_manager_does_not_execute_risk_when_exit_blocked(self):
        self.low_risk()
        with tempfile.TemporaryDirectory() as tmp, patch.object(trade_a, 'ROOT', Path(tmp)), \
             patch.object(trade_a, 'LIVE', False), \
             patch.object(trade_a, 'load', return_value={'BTC': self.p}), \
             patch.object(trade_a, 'exchange', return_value=self.ex), \
             patch.object(self.ex, 'fetch_order_book', side_effect=TimeoutError('offline')), \
             patch.object(trade_a, 'do_close') as close:
            r = manage_a.run(execute=True, account=True)['BTC']
        close.assert_not_called()
        self.assert_known_risk_blocked(r)
        self.assertFalse(r['executed'])

    def test_unknown_spot_balance_blocks_partial_exit(self):
        self.p.update(base=0, phase='closing')
        for value in (None, '', float('nan')):
            with self.subTest(value=value):
                self.ex.balance['BTC']['total'] = value
                r = self.run_check()
                self.assertEqual(r['action'], 'BLOCK')
                self.assertEqual(r['data_status'], 'INCOMPLETE')
                with self.assertRaises((TypeError, ValueError)):
                    trade_a.account(self.ex, self.p)

    def test_missing_spot_balance_blocks_entry(self):
        self.ex.positions = []
        for balance in ({'USDT': {'free': 10000}}, dict(self.ex.balance, BTC={})):
            with self.subTest(balance=balance):
                self.ex.balance = balance
                self.assertEqual(self.run_check(opening=True)['action'], 'BLOCK')

    def test_explicit_zero_spot_balance_allows_confirmed_remainder(self):
        self.p.update(base=0, phase='closing')
        self.ex.balance['BTC']['total'] = 0
        r = self.run_check()
        self.assertEqual(r['action'], 'EXIT')
        self.assertEqual(r['data_status'], 'COMPLETE')

    def test_invalid_open_orders_both_symbols_block(self):
        for symbol in (self.p['spot'], self.p['perp']):
            for value in (None, {}, ''):
                with self.subTest(symbol=symbol, value=value):
                    def orders(requested):
                        return value if requested == symbol else []
                    with patch.object(self.ex, 'fetch_open_orders', side_effect=orders):
                        r = self.run_check()
                        self.assertEqual(r['action'], 'BLOCK')
                        self.assertEqual(r['risk']['status'], 'UNKNOWN')
                        self.assertTrue(any('INVALID_OPEN_ORDERS_RESPONSE' in g for g in r['gaps']))
                        with self.assertRaisesRegex(ValueError, 'INVALID_OPEN_ORDERS_RESPONSE'):
                            trade_a.account(self.ex, self.p)

    def test_empty_mapping_positions_not_empty_account(self):
        with patch.object(self.ex, 'fetch_positions', return_value={}):
            self.assertEqual(self.run_check()['action'], 'BLOCK')
            with self.assertRaisesRegex(ValueError, 'INVALID_POSITIONS_RESPONSE'):
                trade_a.account(self.ex, self.p)

    def test_manager_blocks_missing_balance_without_attempting_close(self):
        self.p.update(base=0, phase='closing')
        self.ex.balance['BTC']['total'] = None
        with tempfile.TemporaryDirectory() as tmp, patch.object(trade_a, 'ROOT', Path(tmp)), \
             patch.object(trade_a, 'LIVE', False), \
             patch.object(trade_a, 'load', return_value={'BTC': self.p}), \
             patch.object(trade_a, 'exchange', return_value=self.ex), \
             patch.object(trade_a, 'do_close') as close:
            r = manage_a.run(execute=True, account=True)['BTC']
        close.assert_not_called()
        self.assertEqual(r['action'], 'BLOCK')
        self.assertFalse(r['executed'])


if __name__ == '__main__':
    unittest.main(verbosity=2)
