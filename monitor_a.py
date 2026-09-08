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
            continue
        try:
            contracts = finite(raw_contracts)
        except (TypeError, ValueError):
            gaps.append('POSITION_CONTRACTS_INVALID')
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
    if isinstance(total, dict):
        for coin, value in total.items():
            try:
                amount = finite(value)
            except (TypeError, ValueError):
                gaps.append(f'BALANCE_INVALID:{coin}')
                continue
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
            'nonzero_balances': nonzero, 'gaps': gaps}


def compare(local_rows, account_rows):
    gaps = []
    local_by_perp = {row.get('perp'): row for row in local_rows if row.get('perp')}
    account_by_symbol = {row.get('symbol'): row for row in account_rows if row.get('symbol')}
    for symbol, row in local_by_perp.items():
        actual = account_by_symbol.get(symbol)
        if not actual:
            gaps.append(f'LOCAL_POSITION_NOT_FOUND:{row["coin"]}')
            continue
        try:
            expected = finite(row['contracts'])
            observed = finite(actual['contracts'])
        except (TypeError, ValueError):
            gaps.append(f'POSITION_COMPARE_INVALID:{row["coin"]}')
            continue
        if abs(expected - observed) > max(1e-8, abs(expected) * 1e-8):
            gaps.append(f'POSITION_CONTRACTS_MISMATCH:{row["coin"]}')
    for symbol in account_by_symbol:
        if symbol not in local_by_perp:
            gaps.append(f'ACCOUNT_POSITION_UNOWNED:{symbol}')
    return gaps


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
        report['gaps'] = account['gaps'] + compare(local_rows, account['positions'])
        if report['gaps']:
            report['status'] = 'DATA_GAP'
        elif not local_data and not account['positions']:
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
