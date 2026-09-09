import copy
import importlib
import json
import math
from pathlib import Path
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, '/Volumes/数据分区/资金费率套利')
import trade_a as t
import scan
import pool
import verify


class Exchange:
    def __init__(self):
        self.markets = {'ZEC/USDT': {'active': True},
                        'ZEC/USDT:USDT': {'active': True, 'contractSize': 1}}
        self.base = 0
        self.short = 0
        self.calls = []
        self.orders = {}
        self.effects = []

    def amount_to_precision(self, sym, n):
        return str(math.floor(n * 1000 + 1e-9) / 1000)

    def fetch_ticker(self, sym):
        return {'last': 100}

    def private_get_account_config(self):
        return {'data': [{'uid': 'offline-test-account', 'acctLv': '2', 'posMode': 'net_mode'}]}

    def fetch_positions(self, syms=None):
        return [{'contracts': self.short, 'side': 'short', 'marginMode': 'cross',
                 'symbol': 'ZEC/USDT:USDT', 'markPrice': 100, 'liquidationPrice': 150}]

    def fetch_balance(self):
        return {'ZEC': {'total': self.base}, 'USDT': {'free': 10000},
                'info': {'data': [{'mgnRatio': '10'}]}}

    def price_to_precision(self, sym, price):
        return str(math.floor(price * 100) / 100)

    def fetch_order_book(self, sym, limit=50):
        return {'timestamp': int(time.time() * 1000), 'asks': [[100.01, 100]], 'bids': [[100, 100]]}

    def fetch_funding_rate(self, sym):
        return {'fundingRate': .0001, 'fundingTimestamp': int(time.time() * 1000) + 3600000}

    def fetch_open_orders(self, sym=None, params=None):
        return []

    def create_order(self, sym, typ, side, n, price, params):
        assert typ == 'ioc' and price > 0
        assert t.load()['ZEC']['pending']['client_id'] == params['clOrdId']
        self.calls.append((sym, side, n, params))
        effect = self.effects.pop(0) if self.effects else {}
        if effect.get('unknown'):
            raise TimeoutError('unknown')
        filled = n * effect.get('fraction', 1)
        fee = {'currency': 'USDT', 'cost': 0}
        if effect.get('base_fee'):
            fee = {'currency': 'ZEC', 'cost': effect['base_fee']}
        delta = filled * (1 if side == 'buy' else -1)
        if ':' in sym:
            self.short -= delta
        else:
            self.base += delta - (fee['cost'] if fee['currency'] == 'ZEC' else 0)
        result = {'id': params['clOrdId'], 'clientOrderId': params['clOrdId'], 'symbol': sym, 'side': side, 'status': effect.get('status', 'closed'),
                  'filled': filled, 'fee': fee, 'average': 100, 'cost': filled * 100}
        if effect.get('no_fee'):
            result['fee'] = None
        self.orders[params['clOrdId']] = result
        if effect.get('timeout'):
            raise TimeoutError('accepted, response lost')
        return {'id': params['clOrdId']}  # ack has no fill!

    def fetch_order(self, ident, sym, params):
        return self.orders[params['clOrdId']]


class ExecutionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = patch.object(t, 'ROOT', Path(self.tmp.name))
        self.root.start()
        locks = patch.object(t, 'ACCOUNT_LOCK_ROOT', Path(self.tmp.name)/'account-locks')
        locks.start(); self.addCleanup(locks.stop)
        self.live = patch.object(t, 'LIVE', True)
        self.live.start()
        self.ex = Exchange()
        self.exchange = patch.object(t, 'exchange', return_value=self.ex)
        self.exchange.start()

    def tearDown(self):
        self.exchange.stop()
        self.live.stop()
        self.root.stop()
        self.tmp.cleanup()

    def test_full_cycle_and_parameters(self):
        t.do_open('ZEC', 200)
        self.assertEqual(t.load()['ZEC']['phase'], 'open')
        t.do_close('ZEC')
        p = t.load()['ZEC']
        self.assertEqual(p['phase'], 'closed')
        self.assertEqual(len(p['orders']), 4)
        self.assertEqual(self.ex.calls[0][3]['tdMode'], 'cash')
        self.assertTrue(self.ex.calls[2][3]['reduceOnly'])

    def test_response_lost_queries_original(self):
        self.ex.effects = [{'timeout': True}, {'timeout': True}]
        t.do_open('ZEC', 200)
        self.assertEqual(len(self.ex.calls), 2)
        self.assertEqual(t.load()['ZEC']['contracts'], 2)

    def test_unknown_blocks_second_leg_and_close(self):
        self.ex.effects = [{'unknown': True}]
        with self.assertRaises(KeyError):
            t.do_open('ZEC', 200)
        with self.assertRaises(RuntimeError):
            t.do_close('ZEC')
        self.assertEqual(len(self.ex.calls), 1)
        self.assertIn('pending', t.load()['ZEC'])

    def test_recover_does_not_resend_or_double_count(self):
        self.ex.effects = [{'status': 'open'}]
        with self.assertRaises(RuntimeError):
            t.do_open('ZEC', 200)
        key = next(iter(self.ex.orders))
        self.ex.orders[key]['status'] = 'closed'
        t.do_recover('ZEC')
        t.do_recover('ZEC')
        self.assertEqual(t.load()['ZEC']['base'], 2)
        self.assertEqual(len(self.ex.calls), 1)
        t.do_close('ZEC')
        self.assertEqual(len(self.ex.calls), 2)

    def test_partial_spot_hedges_actual_quantity(self):
        self.ex.effects = [{'fraction': .5, 'status': 'canceled'}]
        t.do_open('ZEC', 200)
        self.assertEqual(self.ex.calls[1][2], 1)

    def test_base_fee_reduces_hedge(self):
        self.ex.effects = [{'base_fee': .01}]
        t.do_open('ZEC', 200)
        self.assertEqual(self.ex.calls[1][2], 1.99)

    def test_missing_fee_blocks(self):
        self.ex.effects = [{'no_fee': True}]
        with self.assertRaises(RuntimeError):
            t.do_open('ZEC', 200)
        self.assertIn('pending', t.load()['ZEC'])

    def test_partial_close_resumes_remaining(self):
        t.do_open('ZEC', 200)
        self.ex.effects = [{'fraction': .5, 'status': 'canceled'}]
        with self.assertRaises(RuntimeError):
            t.do_close('ZEC')
        self.assertEqual(t.load()['ZEC']['contracts'], 1)
        self.assertEqual(self.ex.base, 2)
        t.do_close('ZEC')
        self.assertEqual(self.ex.calls[3][2], 1)
        self.assertEqual(t.load()['ZEC']['phase'], 'closed')

    def test_account_drift_blocks(self):
        t.do_open('ZEC', 200)
        self.ex.short += 1
        with self.assertRaises(RuntimeError):
            t.do_close('ZEC')
        self.assertEqual(len(self.ex.calls), 2)

    def test_wrong_account_mode_blocks_before_order(self):
        with patch.object(self.ex, 'private_get_account_config', return_value={
                'data': [{'acctLv': '3', 'posMode': 'net_mode'}]}):
            with self.assertRaises(RuntimeError):
                t.do_open('ZEC', 200)
        self.assertFalse(self.ex.calls)

    def test_dry_live_isolation(self):
        t.LIVE = False
        t.do_open('ZEC', 200)
        t.LIVE = True
        with self.assertRaises(KeyError):
            t.do_close('ZEC')
        self.assertFalse(self.ex.calls)
        t.do_open('ZEC', 200)
        t.LIVE = False
        t.do_close('ZEC')
        t.LIVE = True
        self.assertEqual(t.load()['ZEC']['phase'], 'open')

    def test_mode_tag_and_legacy_fail_closed(self):
        t.save({'ZEC': {'live': False}})
        with self.assertRaises(RuntimeError):
            t.load()
        (t.ROOT / 'positions_a.json').write_text('{"ZEC":{}}')
        with self.assertRaises(RuntimeError):
            t.load(reject_legacy=True)

    def test_invalid_notional(self):
        for n in (0, -1, float('nan'), float('inf')):
            with self.assertRaises(ValueError):
                t.do_open('ZEC', n)
        self.assertFalse(self.ex.calls)


def row(ex='a', **changes):
    return dict(coin='BTC', exchange=ex, apr_7d=.2, same_sign=1, positive_share=1,
                data_status='VALID', days=7, vol_usd=20_000_000, spot_vol_usd=20_000_000,
                apr_a_net=.18, taker=.001, settle='USDT') | changes


class DataTests(unittest.TestCase):
    def test_scan_missing_volume_does_not_fetch_history(self):
        ex = scan_exchange()
        ex.fetch_tickers.side_effect = TimeoutError()
        with patch.object(scan.ccxt, 'okx', return_value=ex):
            rows = scan.fetch('okx')
        self.assertEqual(rows[0]['gap_reason'], 'volume_missing')
        self.assertIsNone(rows[0]['apr_7d'])
        self.assertIsNone(rows[0]['vol_usd'])
        ex.fetch_funding_rate_history.assert_not_called()

    def test_scan_valid_and_short_history(self):
        ex = scan_exchange()
        with patch.object(scan.ccxt, 'okx', return_value=ex):
            rows = scan.fetch('okx')
        self.assertTrue(scan.usable(rows[0]))
        self.assertEqual(len(scan.mode_a_candidates(rows)), 1)
        ex.fetch_funding_rate_history.return_value = ex.fetch_funding_rate_history.return_value[-3:]
        with patch.object(scan.ccxt, 'okx', return_value=ex):
            rows = scan.fetch('okx')
        self.assertIsNone(rows[0]['apr_7d'])
        self.assertFalse(scan.mode_a_candidates(rows))

    def test_scan_stale_and_internal_gap(self):
        for mode in ('stale', 'gapped'):
            ex = scan_exchange()
            hist = ex.fetch_funding_rate_history.return_value
            ex.fetch_funding_rate_history.return_value = hist[:-3] if mode == 'stale' else hist[:8] + hist[11:]
            with patch.object(scan.ccxt, 'okx', return_value=ex):
                rows = scan.fetch('okx')
            self.assertIsNone(rows[0]['apr_a_net'])
            self.assertEqual(rows[0]['data_status'], 'INVALID_DECLARED_GAP')

    def test_verify_complete_and_truncated_week(self):
        ex = scan_exchange()
        now = ex.milliseconds()
        pts = [(now - i * 8 * 3600000, .0001) for i in range(84, 0, -1)]
        with patch.object(verify.ccxt, 'okx', return_value=ex), patch.object(verify, 'pull_history', return_value=pts):
            full = verify.fetch('okx')
        self.assertEqual({r[2] for r in full}, {0, 1, 2, 3})
        with patch.object(verify.ccxt, 'okx', return_value=ex), patch.object(verify, 'pull_history', return_value=pts[:-10]):
            short = verify.fetch('okx')
        self.assertNotIn(0, {r[2] for r in short})

    def test_ccxt_okx_payload_offline(self):
        ex = t.ccxt.okx()
        common = {'base': 'ZEC', 'quote': 'USDT', 'active': True,
                  'precision': {'amount': .001, 'price': .01},
                  'option': False, 'future': False, 'type': 'spot', 'linear': None, 'inverse': None}
        ex.set_markets([
            common | {'id': 'ZEC-USDT', 'symbol': 'ZEC/USDT', 'spot': True, 'swap': False, 'contract': False},
            common | {'id': 'ZEC-USDT-SWAP', 'symbol': 'ZEC/USDT:USDT', 'spot': False, 'swap': True,
                      'type': 'swap', 'linear': True, 'contract': True, 'contractSize': 1, 'settle': 'USDT'}])
        req = ex.create_order_request('ZEC/USDT', 'market', 'buy', 2, None,
                    {'tdMode': 'cash', 'tgtCcy': 'base_ccy', 'banAmend': True, 'clOrdId': 'abc'})
        self.assertEqual((req['sz'], req['tgtCcy'], req['tdMode']), ('2', 'base_ccy', 'cash'))
        req = ex.create_order_request('ZEC/USDT:USDT', 'market', 'buy', 2, None,
                    {'tdMode': 'cross', 'posSide': 'net', 'reduceOnly': True, 'clOrdId': 'def'})
        self.assertTrue(req['reduceOnly'])
        self.assertEqual(req['posSide'], 'net')

    def test_pool_short_history_and_missing_volume(self):
        for changes in ({'days': 4}, {'vol_usd': None}, {'data_status': 'INVALID_DECLARED_GAP'}):
            self.assertFalse(pool.candidates([row(), row('b', **changes)]))

    def test_mode_a_requires_positive_share(self):
        self.assertEqual(len(scan.mode_a_candidates([row()])), 1)
        self.assertFalse(scan.mode_a_candidates([row(positive_share=.1)]))

    def test_pair_kept_when_no_longer_best(self):
        self.assertIn(('BTC', 'a', 'b'), pool.candidates([row(), row('b'), row('c', apr_7d=.5)]))

    def test_ambiguous_contracts_skip(self):
        self.assertFalse(pool.candidates([row(), row(), row('b')] ))

    def test_pool_gap_preserves_state_and_blank_estimate(self):
        with tempfile.TemporaryDirectory() as tmp:
            state, log = Path(tmp) / 'p.json', Path(tmp) / 'p.csv'
            position = {'coin': 'BTC', 'short_on': 'a', 'long_on': 'b', 'opened_ts': 0, 'entry_net': .1}
            state.write_text(json.dumps({'positions': [position]}))
            with patch.object(pool, 'STATE', str(state)), patch.object(pool, 'LOG', str(log)), \
                 patch.object(pool, 'EXCHANGES', ['a']), patch.object(pool, 'fetch', return_value=[]):
                pool.main()
            self.assertEqual(json.loads(state.read_text())['positions'], [position])
            self.assertTrue(log.read_text().splitlines()[-1].endswith(','))

    def test_verify_failure_does_not_return_partial_history(self):
        ex = unittest.mock.Mock()
        ex.fetch_funding_rate_history.side_effect = [[{'timestamp': 10, 'fundingRate': .1}], TimeoutError()]
        self.assertEqual(verify.pull_history(ex, 'BTC/USDT:USDT', 0), [])


def scan_exchange():
    ex = unittest.mock.Mock()
    ex.markets = {
        'ZEC/USDT': {'symbol': 'ZEC/USDT', 'spot': True, 'base': 'ZEC', 'quote': 'USDT'},
        'ZEC/USDT:USDT': {'symbol': 'ZEC/USDT:USDT', 'swap': True, 'linear': True,
                          'base': 'ZEC', 'settle': 'USDT', 'contractSize': 1}}
    ex.has = {'fetchFundingRates': True}
    ex.fetch_funding_rates.return_value = {'ZEC/USDT:USDT': {'fundingRate': .0001, 'interval': '8h'}}
    ex.fetch_tickers.return_value = {s: {'last': 100, 'baseVolume': 200000, 'quoteVolume': 20000000}
                                     for s in ex.markets}
    now = 100 * 86400000
    ex.milliseconds.return_value = now
    ex.fetch_funding_rate_history.return_value = [
        {'timestamp': now - i * 8 * 3600000, 'fundingRate': .0001} for i in range(21, 0, -1)]
    return ex


if __name__ == '__main__':
    unittest.main()
