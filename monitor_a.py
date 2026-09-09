"""模式 A 只读持仓监控；轮询账户并写快照，不包含下单或平仓路径。"""
import argparse
import fcntl
import json
import math
import os
import signal
import time

import trade_a


ACCOUNT_ENV = ('OKX_API_KEY', 'OKX_SECRET', 'OKX_PASSWORD')
DEFAULT_INTERVAL = 60.0
MIN_INTERVAL = 10.0
STOP = False


def finite(value):
    n = float(value)
    if not math.isfinite(n):
        raise ValueError('NONFINITE_DATA')
    return n


def read_local(account_mode):
    """只在读取状态时持共享锁，不把网络查询放进交易锁。"""
    trade_a.LIVE = account_mode
    path = trade_a.state_path()
    with path.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_SH)
        data = trade_a.load()
    rows = []
    for coin, position in data.items():
        rows.append({'coin': coin, 'phase': position.get('phase'),
                     'spot': position.get('spot'), 'perp': position.get('perp'),
                     'base': position.get('base'), 'contracts': position.get('contracts'),
                     'baseline': position.get('baseline'),
                     'pending': bool(position.get('pending'))})
    return data, rows


def account_snapshot(ex):
    gaps = []
    positions = []
    raw_positions = ex.fetch_positions()
    if not isinstance(raw_positions, list):
        raise ValueError('INVALID_POSITIONS_RESPONSE')
    for position in raw_positions:
        if not isinstance(position, dict):
            gaps.append('POSITION_ROW_INVALID')
            continue
        raw_contracts = position.get('contracts')
        if raw_contracts in (None, ''):
            gaps.append('POSITION_CONTRACTS_UNKNOWN:' + str(position.get('symbol')))
            positions.append({'symbol': position.get('symbol'), 'side': position.get('side'),
                              'contracts': None})
            continue
        try:
            contracts = finite(raw_contracts)
        except (TypeError, ValueError):
            gaps.append('POSITION_CONTRACTS_INVALID')
            positions.append({'symbol': position.get('symbol'), 'side': position.get('side'),
                              'contracts': None})
            continue
        if abs(contracts) <= 1e-12:
            continue
        symbol = position.get('symbol')
        if not symbol:
            gaps.append('POSITION_SYMBOL_UNKNOWN')
            continue
        row = {'symbol': symbol, 'side': position.get('side'), 'contracts': contracts}
        for key in ('markPrice', 'liquidationPrice', 'leverage', 'marginMode'):
            if position.get(key) is not None:
                row[key] = position[key]
        if position.get('markPrice') is None or position.get('liquidationPrice') is None:
            gaps.append(f'LIQUIDATION_DATA_UNKNOWN:{symbol}')
        positions.append(row)

    balance = ex.fetch_balance()
    usdt = balance.get('USDT') or {}
    balance_view = {}
    for key in ('free', 'used', 'total'):
        if usdt.get(key) is None:
            gaps.append(f'USDT_{key.upper()}_UNKNOWN')
        else:
            balance_view[key] = finite(usdt[key])
    total = balance.get('total')
    nonzero = {}
    totals = {}
    if isinstance(total, dict):
        for coin, value in total.items():
            try:
                amount = finite(value)
            except (TypeError, ValueError):
                gaps.append(f'BALANCE_INVALID:{coin}')
                continue
            totals[coin] = amount
            if abs(amount) > 1e-12:
                nonzero[coin] = amount
    else:
        gaps.append('BALANCE_TOTAL_UNKNOWN')

    info = balance.get('info') or {}
    info_rows = info.get('data') if isinstance(info, dict) else None
    if not isinstance(info_rows, list) or not info_rows:
        gaps.append('ACCOUNT_INFO_UNKNOWN')
    else:
        margin_ratio = info_rows[0].get('mgnRatio')
        if margin_ratio in (None, ''):
            gaps.append('MARGIN_RATIO_UNKNOWN')
        else:
            balance_view['margin_ratio'] = finite(margin_ratio)
    return {'positions': positions, 'balance': balance_view,
            'nonzero_balances': nonzero, 'balance_totals': totals, 'gaps': gaps}


def compare(local_rows, account_rows, balance_totals=None):
    """仅比较确认余量；余额缺键不是零，历史关闭记录不认领新仓。"""
    gaps = []
    active = []
    for row in local_rows:
        coin = row.get('coin')
        if row.get('pending'):
            gaps.append(f'LOCAL_PENDING_UNKNOWN:{coin}')
        try:
            base, contracts = finite(row.get('base')), finite(row.get('contracts'))
            if base < 0 or contracts < 0:
                raise ValueError('negative remainder')
        except (TypeError, ValueError):
            gaps.append(f'LOCAL_QUANTITY_UNKNOWN:{coin}')
            active.append(row)
            continue
        if row.get('phase') == 'closed':
            if base == 0 and contracts == 0 and not row.get('pending'):
                continue
            gaps.append(f'LOCAL_CLOSED_WITH_REMAINDER:{coin}')
        active.append(row)

    local_by_perp = {}
    account_by_symbol = {}
    for row in active:
        symbol = row.get('perp')
        if not symbol:
            gaps.append(f'LOCAL_SYMBOL_UNKNOWN:{row.get("coin")}')
        local_by_perp.setdefault(symbol, []).append(row)
    for row in account_rows:
        symbol = row.get('symbol')
        account_by_symbol.setdefault(symbol, []).append(row)
    for symbol, rows in local_by_perp.items():
        if len(rows) != 1:
            gaps.append(f'LOCAL_POSITION_AMBIGUOUS:{symbol}')
            continue
        row = rows[0]
        coin = row.get('coin')
        actuals = account_by_symbol.get(symbol, [])
        if len(actuals) > 1:
            gaps.append(f'ACCOUNT_POSITION_AMBIGUOUS:{symbol}')
        for actual in actuals:
            try:
                observed = finite(actual.get('contracts'))
                if observed != 0 and actual.get('side') != 'short':
                    gaps.append(f'POSITION_SIDE_MISMATCH:{coin}')
                if observed < 0:
                    raise ValueError('negative contracts')
            except (TypeError, ValueError):
                gaps.append(f'POSITION_COMPARE_INVALID:{coin}')
        try:
            expected = finite(row.get('contracts'))
            observed = sum(finite(a.get('contracts')) for a in actuals)
        except (TypeError, ValueError):
            gaps.append(f'POSITION_COMPARE_INVALID:{coin}')
            continue
        if expected > 0 and not actuals:
            gaps.append(f'LOCAL_POSITION_NOT_FOUND:{coin}')
        elif abs(expected - observed) > max(1e-8, abs(expected) * 1e-8):
            gaps.append(f'POSITION_CONTRACTS_MISMATCH:{coin}')
    for symbol in account_by_symbol:
        if symbol not in local_by_perp:
            gaps.append(f'ACCOUNT_POSITION_UNOWNED:{symbol}')

    spot_owners = {}
    for row in active:
        spot = row.get('spot')
        if not isinstance(spot, str) or '/' not in spot:
            gaps.append(f'SPOT_SYMBOL_UNKNOWN:{row.get("coin")}')
            continue
        spot_owners.setdefault(spot.split('/')[0], []).append(row)
    for coin, rows in spot_owners.items():
        if len(rows) != 1:
            gaps.append(f'SPOT_OWNERSHIP_AMBIGUOUS:{coin}')
            continue
        try:
            baseline = finite(rows[0].get('baseline'))
            base = finite(rows[0].get('base'))
            if baseline < 0 or base < 0:
                raise ValueError('negative spot')
            expected = baseline + base
        except (TypeError, ValueError):
            gaps.append(f'SPOT_OWNERSHIP_UNKNOWN:{coin}')
            continue
        try:
            actual = finite((balance_totals or {}).get(coin))
        except (TypeError, ValueError):
            gaps.append(f'SPOT_BALANCE_UNKNOWN:{coin}')
            continue
        if abs(actual - expected) > max(1e-10, abs(expected) * 1e-8):
            gaps.append(f'SPOT_BALANCE_MISMATCH:{coin}')
    # 现金与未参与策略的其他资产不自动认领为策略现货。
    for coin, amount in (balance_totals or {}).items():
        if coin != 'USDT' and coin not in spot_owners and amount != 0:
            gaps.append(f'ACCOUNT_BALANCE_UNOWNED:{coin}')
    return list(dict.fromkeys(gaps))


def finish(report, exchange_instance, started):
    report['elapsed_sec'] = round(time.monotonic() - started, 3)
    return report, exchange_instance


def status_path():
    return trade_a.ROOT / 'monitor_a_status.json'


def snapshot(account_mode, exchange_instance=None):
    started = time.monotonic()
    report = {'checked_at': time.time(),
              'mode': 'account_read_only' if account_mode else 'local_only',
              'local_positions': [], 'gaps': []}
    try:
        local_data, local_rows = read_local(account_mode)
    except Exception as exc:
        report['status'] = 'ERROR'
        report['gaps'] = ['LOCAL_STATE_ERROR:' + type(exc).__name__]
        return finish(report, None, started)
    report['local_positions'] = local_rows
    if not account_mode:
        report['status'] = 'OK' if local_rows else 'NO_LOCAL_POSITIONS'
        return finish(report, exchange_instance, started)

    missing = [name for name in ACCOUNT_ENV if not os.environ.get(name)]
    if missing:
        report['status'] = 'CONFIG_MISSING'
        report['gaps'] = ['MISSING_CREDENTIALS:' + ','.join(missing)]
        return finish(report, None, started)
    try:
        if exchange_instance is None:
            exchange_instance = trade_a.exchange()
        account = account_snapshot(exchange_instance)
        report.update({'account_positions': account['positions'],
                       'balance': account['balance'],
                       'nonzero_balances': account['nonzero_balances']})
        report['gaps'] = account['gaps'] + compare(local_rows, account['positions'], account['balance_totals'])
        if report['gaps']:
            report['status'] = 'DATA_GAP'
        elif not account['positions'] and all(
                row.get('phase') == 'closed' and row.get('base') == 0
                and row.get('contracts') == 0 and not row.get('pending')
                for row in local_rows):
            report['status'] = 'NO_POSITIONS'
        else:
            report['status'] = 'OK'
        return finish(report, exchange_instance, started)
    except Exception as exc:
        report['status'] = 'ERROR'
        report['gaps'] = ['API_ERROR:' + type(exc).__name__]
        return finish(report, None, started)


def write_status(report):
    path = status_path()
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as handle:
        json.dump(report, handle, indent=2, ensure_ascii=False, allow_nan=False)
        handle.flush()
        os.fchmod(handle.fileno(), 0o600)
        os.fsync(handle.fileno())
    os.replace(tmp, path)
    directory = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(directory)
    finally:
        os.close(directory)


def stop_handler(signum, frame):
    global STOP
    STOP = True


def run(account_mode=False, interval=DEFAULT_INTERVAL, once=False):
    global STOP
    STOP = False
    exchange_instance = None
    while True:
        report, exchange_instance = snapshot(account_mode, exchange_instance)
        write_status(report)
        print(json.dumps(report, ensure_ascii=False, sort_keys=True), flush=True)
        if once or STOP:
            return 0
        wait_until = time.monotonic() + max(0, interval - report.get('elapsed_sec', 0))
        while not STOP:
            remaining = wait_until - time.monotonic()
            if remaining <= 0:
                break
            time.sleep(min(1.0, remaining))
        if STOP:
            return 0


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--account', action='store_true',
                        help='只读 OKX 账户；需要三个 OKX_* 环境变量')
    parser.add_argument('--interval', type=float, default=DEFAULT_INTERVAL,
                        help='轮询间隔秒数，至少 10 秒')
    parser.add_argument('--once', action='store_true', help='只执行一次并退出')
    args = parser.parse_args()
    if not math.isfinite(args.interval) or args.interval < MIN_INTERVAL:
        parser.error(f'--interval 必须是不小于 {MIN_INTERVAL:g} 的有限数')
    signal.signal(signal.SIGTERM, stop_handler)
    signal.signal(signal.SIGINT, stop_handler)
    return run(args.account, args.interval, args.once)


if __name__ == '__main__':
    raise SystemExit(main())
