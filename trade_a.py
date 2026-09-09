"""OKX 模式 A：默认空跑，recover 仅查询原单，close 按确认余量退出。"""
import argparse
from contextlib import contextmanager, ExitStack
from functools import wraps
import hashlib
import re
import threading
import fcntl
import json
import math
import os
from pathlib import Path
import time
import uuid

import ccxt
from scan import PROXY, fetch, mode_a_candidates

LIVE = False
ROOT = Path(__file__).resolve().parent


# 同一操作系统用户下，各项目副本共用账户锁；不充当跨主机分布式锁。
ACCOUNT_LOCK_ROOT = Path.home() / '.local/state/funding-arb/execution-locks'
_SESSION = None


def session():
    if (_SESSION is None or _SESSION['owner'] != (os.getpid(), threading.get_ident())
            or _SESSION['path'] != str(state_path().resolve())):
        raise RuntimeError('EXECUTION_SESSION_REQUIRED')
    return _SESSION


@contextmanager
def execution_session():
    global _SESSION
    if _SESSION is not None:
        yield session()
        return
    with state_path().with_suffix('.lock').open('a') as local_lock:
        os.fchmod(local_lock.fileno(), 0o600)
        fcntl.flock(local_lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        ex = exchange()
        account_key = 'dry'
        with ExitStack() as stack:
            if LIVE:
                config = ex.private_get_account_config()['data'][0]
                uid = config.get('uid')
                if not isinstance(uid, str) or not uid:
                    raise RuntimeError('ACCOUNT_ID_UNKNOWN')
                account_key = hashlib.sha256(('okx:' + uid).encode()).hexdigest()
                missing = []
                parent = ACCOUNT_LOCK_ROOT
                while not parent.exists():
                    missing.append(parent)
                    parent = parent.parent
                ACCOUNT_LOCK_ROOT.mkdir(parents=True, exist_ok=True, mode=0o700)
                fd = os.open(ACCOUNT_LOCK_ROOT / (account_key + '.lock'),
                             os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
                handle = stack.enter_context(os.fdopen(fd, 'r+'))
                os.fchmod(fd, 0o600)
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
                owner = {'version': 1, 'state_path': str(state_path().resolve())}
                raw = handle.read()
                if raw:
                    if json.loads(raw) != owner:
                        raise RuntimeError('ACCOUNT_BOUND_TO_ANOTHER_STATE; explicit migration required')
                else:
                    # 锁文件保留路径归属，不能删除来绕过另一副本的状态。
                    json.dump(owner, handle); handle.flush(); os.fsync(fd)
                    for directory in [ACCOUNT_LOCK_ROOT, *(p.parent for p in missing)]:
                        directory_fd = os.open(directory, os.O_RDONLY)
                        try:
                            os.fsync(directory_fd)
                        finally:
                            os.close(directory_fd)
            _SESSION = {'owner': (os.getpid(), threading.get_ident()),
                        'path': str(state_path().resolve()), 'account_key': account_key, 'ex': ex}
            try:
                yield _SESSION
            finally:
                _SESSION = None


def executing(fn):
    @wraps(fn)
    def guarded(*args, **kwargs):
        with execution_session():
            return fn(*args, **kwargs)
    return guarded


def validate_state(d):
    ids = set()
    order_ids = set()
    key = session()['account_key']
    for p in d.values():
        if p.get('state_version') != 1:
            raise RuntimeError('LEGACY_STATE_REVIEW_REQUIRED; no automatic migration')
        ident = p.get('trade_id')
        if not isinstance(ident, str) or not re.fullmatch('[0-9a-f]{32}', ident) or ident in ids:
            raise RuntimeError('INVALID_OR_DUPLICATE_TRADE_ID')
        ids.add(ident)
        for field in ('base', 'contracts'):
            if number(p['targets'][field]) <= 0:
                raise RuntimeError('INVALID_TARGET_QUANTITY')
        for order in [*p['orders'], *([p['pending']] if p.get('pending') else [])]:
            client_id = order.get('client_id')
            if (not isinstance(client_id, str) or not re.fullmatch('[0-9a-f]{32}', client_id)
                    or client_id in order_ids or order.get('trade_id') != ident):
                raise RuntimeError('INVALID_OR_DUPLICATE_ORDER_INTENT')
            order_ids.add(client_id)
            leg = order.get('leg')
            if (leg not in ('base', 'contracts') or order.get('side') not in ('buy', 'sell')
                    or order.get('symbol') != p['spot' if leg == 'base' else 'perp']
                    or number(order['amount']) <= 0):
                raise RuntimeError('INVALID_ORDER_INTENT')
        previous = p.get('previous')
        while previous:
            old_id = previous.get('trade_id')
            if old_id in ids:
                raise RuntimeError('DUPLICATE_HISTORICAL_TRADE_ID')
            if old_id:
                ids.add(old_id)
            for old_order in previous.get('orders', []):
                old_client_id = old_order.get('client_id')
                if old_client_id:
                    if old_client_id in order_ids:
                        raise RuntimeError('DUPLICATE_HISTORICAL_ORDER_INTENT')
                    order_ids.add(old_client_id)
            previous = previous.get('previous')
        if p.get('account_key') != key:
            raise RuntimeError('STATE_ACCOUNT_MISMATCH')
        if p.get('phase') not in ('opening', 'open', 'closing', 'closed', 'needs_close'):
            raise RuntimeError('INVALID_EXECUTION_PHASE')
        for field in ('base', 'contracts', 'baseline', 'size'):
            if number(p[field]) < 0 or (field == 'size' and number(p[field]) == 0):
                raise RuntimeError('INVALID_STATE_QUANTITY')
        if p['phase'] == 'closed' and (p['base'] or p['contracts'] or p.get('pending')):
            raise RuntimeError('CLOSED_STATE_INCONSISTENT')
        if p.get('paused'):
            raise RuntimeError('LOCAL_EXECUTION_PAUSED')


def startup(ex, d, opening=False):
    """全局启动门槛；不补单、不撤单、不改账户，不将未完成动作当作可新开仓。"""
    validate_state(d)
    for p in d.values():
        if p.get('pending'):
            raise RuntimeError('PENDING_ORDER_RECOVER_FIRST:' + p['trade_id'])
        if opening and p['phase'] not in ('open', 'closed'):
            raise RuntimeError('RECOVERY_REQUIRED:' + p['trade_id'])
    if not LIVE:
        return
    config = ex.private_get_account_config()['data'][0]
    current_key = hashlib.sha256(('okx:' + str(config.get('uid', ''))).encode()).hexdigest()
    if current_key != session()['account_key']:
        raise RuntimeError('ACCOUNT_ID_CHANGED')
    if config.get('posMode') != 'net_mode' or config.get('acctLv') != '2':
        raise RuntimeError('ACCOUNT_MODE_UNSUPPORTED; requires acctLv=2 net_mode')
    # 任一挂单即阻止，无需翻页证明非空；空页才可继续。覆盖普通与条件委托。
    for params in ({}, *({'ordType': kind} for kind in
                        ('conditional', 'oco', 'trigger', 'move_order_stop', 'iceberg', 'twap'))):
        orders = ex.fetch_open_orders(None, params=params)
        if not isinstance(orders, list):
            raise RuntimeError('OPEN_ORDERS_UNKNOWN')
        if orders:
            raise RuntimeError('ACCOUNT_OPEN_ORDERS_REQUIRE_REVIEW')
    positions = ex.fetch_positions()
    if not isinstance(positions, list):
        raise RuntimeError('ACCOUNT_POSITIONS_UNKNOWN')
    active = [p for p in d.values() if p['phase'] != 'closed']
    symbols = [p['perp'] for p in active]
    if len(symbols) != len(set(symbols)):
        raise RuntimeError('AMBIGUOUS_LOCAL_OWNERSHIP')
    for row in positions:
        qty = number(row['contracts'])
        if qty < 0:
            raise RuntimeError('INVALID_ACCOUNT_QUANTITY')
        if qty and row.get('symbol') not in symbols:
            raise RuntimeError('ACCOUNT_POSITION_UNOWNED')
    for p in active:
        reconcile(ex, p)


def refresh_state(p):
    if p.get('state_version') != 1:
        return
    pending = p.get('pending')
    p['execution_state'] = ('PAUSED' if p.get('paused') else
        ('PENDING_KNOWN' if pending.get('state') == 'OPEN' else 'PENDING_UNKNOWN') if pending else
        {'opening': 'RECOVERY_REQUIRED', 'needs_close': 'RECOVERY_REQUIRED',
         'closing': 'RECOVERY_REQUIRED', 'open': 'OPEN', 'closed': 'CLOSED'}[p['phase']])
    p['confirmed_remaining'] = {'base': p['base'], 'contracts': p['contracts']}
    p['exposure_base'] = p['base'] - p['contracts'] * p['size']
    p['updated_at'] = time.time()
    fees = {}
    for order in p['orders']:
        result = order.get('result', {})
        for fee in result.get('fees') or ([result['fee']] if result.get('fee') else []):
            fees[fee['currency']] = fees.get(fee['currency'], 0) + number(fee['cost'])
    p['fees_by_currency'] = fees if p['live'] else None


@executing
def do_startup():
    d = load(reject_legacy=LIVE)
    try:
        startup(session()['ex'], d, opening=True)
        result = {'status': 'READY', 'local_records': len(d)}
    except Exception as exc:
        result = {'status': 'PAUSED', 'reason': str(exc).split(':', 1)[0], 'local_records': len(d)}
    print(json.dumps(result))
    return result


def state_path():
    return ROOT / ('positions_a_live.json' if LIVE else 'positions_a_dry.json')


def load(reject_legacy=False):
    legacy = ROOT / 'positions_a.json'
    if reject_legacy and legacy.exists() and json.loads(legacy.read_text()):
        raise RuntimeError('旧 positions_a.json 有记录，须人工核对后迁移；不会自动转换')
    path = state_path()
    d = json.loads(path.read_text()) if path.exists() else {}
    if any(p.get('live') is not LIVE for p in d.values()):
        raise RuntimeError('状态模式不匹配')
    return d


def save(d):
    for position in d.values():
        refresh_state(position)
    path = state_path()
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
        os.fchmod(f.fileno(), 0o600)
        json.dump(d, f, indent=2, ensure_ascii=False, allow_nan=False)
        f.flush()
        os.fsync(f.fileno())
    os.replace(tmp, path)
    fd = os.open(path.parent, os.O_RDONLY)
    try:
        os.fsync(fd)
    finally:
        os.close(fd)


def exchange():
    cfg = {'timeout': 30000}
    if LIVE:
        for key, env in [('apiKey', 'OKX_API_KEY'), ('secret', 'OKX_SECRET'),
                         ('password', 'OKX_PASSWORD')]:
            cfg[key] = os.environ[env]
    if PROXY:
        cfg['httpsProxy'] = PROXY
    ex = ccxt.okx(cfg)
    ex.load_markets()
    return ex


def number(value):
    value = float(value)
    if not math.isfinite(value):
        raise ValueError('非有限数值')
    return value


def amount(ex, sym, qty):
    qty = number(ex.amount_to_precision(sym, qty))
    minimum = ex.markets[sym].get('limits', {}).get('amount', {}).get('min') or 0
    if qty <= 0 or qty < minimum:
        raise RuntimeError(f'{sym} 不足最小下单量，保留余量待人工处理')
    return qty


def account(ex, p):
    config = ex.private_get_account_config()['data'][0]
    if config.get('posMode') != 'net_mode' or config.get('acctLv') != '2':
        raise RuntimeError('仅支持 Futures mode (acctLv=2) + net_mode；不会自动改账户')
    short = 0.0
    positions = ex.fetch_positions([p['perp']])
    if not isinstance(positions, list):
        raise ValueError('INVALID_POSITIONS_RESPONSE')
    for pos in positions:
        n = number(pos['contracts'])
        if n < 0:
            raise ValueError('NEGATIVE_CONTRACTS')
        if n and (pos.get('side') != 'short' or pos.get('marginMode') != 'cross'):
            raise RuntimeError('发现非 cross 空头仓位，停止')
        short += n
    balance = ex.fetch_balance()
    coin = p['spot'].split('/')[0]
    # 缺失余额不能推断为零；显式的数值零仍有效。
    total = number(balance[coin]['total'])
    if total < 0:
        raise ValueError('NEGATIVE_SPOT_BALANCE')
    for symbol in (p['spot'], p['perp']):
        orders = ex.fetch_open_orders(symbol)
        if not isinstance(orders, list):
            raise ValueError('INVALID_OPEN_ORDERS_RESPONSE:' + symbol)
        if orders:
            raise RuntimeError('账户有未完成订单，停止新动作')
    return total, short


def reconcile(ex, p):
    total, short = account(ex, p)
    expected = p['baseline'] + p['base']
    if abs(total - expected) > max(1e-10, abs(expected) * 1e-8) or abs(short - p['contracts']) > 1e-8:
        raise RuntimeError(f'账户不符：现货 {total}/{expected}，空单 {short}/{p["contracts"]}；需人工核对')
    return total, short


def resolve(ex, d, p):
    """按落盘 ID 查询；未找到不等于未成交，保留 pending。"""
    if ex is not session()['ex'] or not any(row is p for row in d.values()):
        raise RuntimeError('EXECUTION_CONTEXT_MISMATCH')
    validate_state(d)
    o = p.get('pending')
    if not o:
        return
    result = ex.fetch_order(None, o['symbol'], {'clOrdId': o['client_id']})
    if result.get('clientOrderId') != o['client_id']:
        raise RuntimeError('客户订单号不匹配')
    if result.get('symbol') != o['symbol'] or result.get('side') != o['side']:
        raise RuntimeError('订单身份不匹配')
    if not result.get('id') or (o.get('exchange_order_id') and result['id'] != o['exchange_order_id']):
        raise RuntimeError('订单 ID 不匹配或缺失')
    o['exchange_order_id'] = result['id']
    if result.get('status') not in ('closed', 'canceled', 'expired', 'rejected'):
        o['state'] = 'OPEN' if result.get('status') == 'open' else 'UNKNOWN'
        o['observed_filled'] = number(result['filled']) if result.get('filled') is not None else None
        p['decision_reason'] = 'ORDER_NOT_TERMINAL'
        save(d)
        raise RuntimeError('订单尚未终结；运行 recover 查询，禁止继续另一腿')
    filled = number(result['filled'])
    if filled < 0 or filled > o['amount'] + 1e-8:
        raise RuntimeError('订单成交数量异常')
    fees = result.get('fees') or ([result['fee']] if result.get('fee') else [])
    if filled and (not fees or any(f.get('cost') is None or not f.get('currency') for f in fees)):
        raise RuntimeError('手续费资料不全，保留 pending 待核对')
    delta = filled if o['side'] == 'buy' else -filled
    if o['leg'] == 'base':
        coin = p['spot'].split('/')[0]
        delta -= sum(number(f['cost']) for f in fees if f['currency'] == coin)
    else:
        delta = -delta
    remaining = p[o['leg']] + delta
    if remaining < -1e-8:
        raise RuntimeError('成交导致负余量，需人工核对')
    o['state'] = 'TERMINAL'
    p['decision_reason'] = 'ORDER_TERMINAL_CONFIRMED'
    p[o['leg']] = max(0, remaining)
    p['orders'].append({**o, 'result': {k: result.get(k) for k in
                        ('id', 'status', 'filled', 'average', 'cost', 'fee', 'fees', 'timestamp')}})
    del p['pending']
    save(d)


def order(ex, d, p, leg, side, qty):
    if ex is not session()['ex'] or not any(row is p for row in d.values()):
        raise RuntimeError('EXECUTION_CONTEXT_MISMATCH')
    validate_state(d)
    if p.get('pending'):
        raise RuntimeError('存在待确认订单，请先 recover')
    sym = p['spot'] if leg == 'base' else p['perp']
    qty = amount(ex, sym, qty)
    if not LIVE:
        p[leg] += qty * (1 if (leg == 'base') == (side == 'buy') else -1)
        p[leg] = max(0, p[leg])
        p['orders'].append({'client_id': uuid.uuid4().hex, 'trade_id': p['trade_id'],
                            'symbol': sym, 'side': side, 'leg': leg, 'amount': qty,
                            'state': 'SIMULATED', 'simulated': True})
        save(d)
        return
    from check_a import execution
    quote = execution(ex, sym, side, qty)
    o = {'client_id': uuid.uuid4().hex, 'symbol': sym, 'side': side,
         'leg': leg, 'amount': qty, 'submitted_at': time.time(),
         'trade_id': p['trade_id'], 'state': 'SUBMIT_INTENT'}
    o['execution_quote'] = quote
    p['pending'] = o
    p['decision_reason'] = 'ORDER_INTENT_DURABLE'
    validate_state(d)
    save(d)
    params = {'clOrdId': o['client_id']}
    params.update({'tdMode': 'cash', 'tgtCcy': 'base_ccy', 'banAmend': True}
                  if leg == 'base' else
                  {'tdMode': 'cross', 'posSide': 'net', 'reduceOnly': side == 'buy'})
    try:
        ack = ex.create_order(sym, 'ioc', side, qty, quote['limit_price'], params)
    except Exception:
        # 响应异常也只查询原单；不重发、不盲目回滚。
        o['state'] = 'UNKNOWN'
        p['decision_reason'] = 'SUBMISSION_RESPONSE_UNKNOWN'
        save(d)
        resolve(ex, d, p)
        return
    o['state'] = 'ACKNOWLEDGED'
    if isinstance(ack, dict) and ack.get('id'):
        o['exchange_order_id'] = ack['id']
    save(d)
    resolve(ex, d, p)


def plan(ex, coin, notional):
    notional = number(notional)
    if notional <= 0:
        raise ValueError('名义金额必须为正')
    spot, perp = f'{coin}/USDT', f'{coin}/USDT:USDT'
    for sym in (spot, perp):
        if sym not in ex.markets or ex.markets[sym].get('active') is False:
            raise RuntimeError(f'市场不可用：{sym}')
    px = number(ex.fetch_ticker(spot)['last'])
    size = number(ex.markets[perp]['contractSize'])
    if px <= 0 or size <= 0:
        raise ValueError('价格与 contractSize 必须为正')
    contracts = amount(ex, perp, notional / px / size)
    base = amount(ex, spot, contracts * size)
    if abs(base - contracts * size) * px > notional * .02:
        raise RuntimeError('计划敞口超过名义 2%')
    for sym, qty in ((spot, base), (perp, contracts)):
        minimum = ex.markets[sym].get('limits', {}).get('cost', {}).get('min') or 0
        if qty * px * (size if sym == perp else 1) < minimum:
            raise RuntimeError(f'{sym} 名义低于最小下单额')
    return {'state_version': 1, 'trade_id': uuid.uuid4().hex,
         'targets': {'base': base, 'contracts': contracts}, 'decision_reason': 'PLAN_CREATED',
         'live': LIVE, 'spot': spot, 'perp': perp, 'base': base, 'contracts': contracts,
         'size': size, 'baseline': 0., 'phase': 'opening', 'orders': [],
         'notional': notional, 'entry_px': px, 'opened_at': time.time()}


@executing
def do_open(coin, notional):
    from check_a import check
    d = load(reject_legacy=LIVE)
    if coin in d and d[coin]['phase'] != 'closed':
        raise RuntimeError('已有记录，请 recover / close')
    ex = session()['ex']
    startup(ex, d, opening=True)
    p = plan(ex, coin, notional)
    p['account_key'] = session()['account_key']
    readiness = check(ex, p, opening=True, private=LIVE)
    if readiness['action'] != 'PASS':
        raise RuntimeError(f'开仓检查未通过：{readiness}')
    base, contracts, size, px = p['base'], p['contracts'], p['size'], p['entry_px']
    p['base'], p['contracts'] = 0., 0.
    p['entry_check'] = readiness
    if LIVE:
        p['baseline'], existing = account(ex, p)
        if existing:
            raise RuntimeError('已有该永续仓位，不能混用')
    if coin in d:
        p['previous'] = d[coin]
    d[coin] = p
    save(d)
    order(ex, d, p, 'base', 'buy', base)
    if not p['base']:
        p['phase'] = 'closed'
        p['closed_at'] = time.time()
        save(d)
        return
    if LIVE:
        reconcile(ex, p)
    contracts = amount(ex, p['perp'], p['base'] / size)
    order(ex, d, p, 'contracts', 'sell', contracts)
    if LIVE:
        reconcile(ex, p)
    p['phase'] = ('open' if p['contracts'] > 0
                  and abs(p['base'] - p['contracts'] * size) <= p['base'] * .02 else 'needs_close')
    p['decision_reason'] = 'HEDGE_CONFIRMED' if p['phase'] == 'open' else 'HEDGE_REQUIRES_REVIEW'
    save(d)
    print(coin, p['phase'], '现货', p['base'], '空单张数', p['contracts'])


@executing
def do_close(coin, *, expected_trade_id=None):
    d = load(reject_legacy=LIVE)
    validate_state(d)
    p = d[coin]
    if expected_trade_id is not None and p['trade_id'] != expected_trade_id:
        raise RuntimeError('TRADE_CHANGED_SINCE_DECISION')
    if p['phase'] == 'closed' and not p.get('pending') and not p['base'] and not p['contracts']:
        print(coin, '已经平仓，保留原平仓时间')
        return
    if p.get('pending'):
        raise RuntimeError('先运行 recover 确认原订单')
    ex = session()['ex']
    startup(ex, d)
    if LIVE:
        reconcile(ex, p)
        from check_a import execution
        # 在拆掉任一腿前，确认两腿当前均可在价格上限内退出。
        for leg, side in [('base', 'sell'), ('contracts', 'buy')]:
            if p[leg] > 1e-10:
                sym = p['spot'] if leg == 'base' else p['perp']
                execution(ex, sym, side, amount(ex, sym, p[leg]))
    p['phase'] = 'closing'
    p['decision_reason'] = 'EXPLICIT_CLOSE_REQUEST'
    save(d)
    if p['contracts'] > 1e-10:
        order(ex, d, p, 'contracts', 'buy', p['contracts'])
        if p['contracts'] > 1e-10:
            raise RuntimeError('永续未全平，余量已保存，再次 close 可继续')
    if LIVE:
        reconcile(ex, p)
    if p['base'] > 1e-10:
        order(ex, d, p, 'base', 'sell', p['base'])
    if LIVE:
        reconcile(ex, p)
    if p['base'] > 1e-10:
        raise RuntimeError('现货仍有余量（可能为精度尘埃），记录保留')
    p['phase'] = 'closed'
    p['closed_at'] = time.time()
    p['decision_reason'] = 'CLOSE_CONFIRMED'
    save(d)
    print(coin, '已平仓；成交记录保留')


@executing
def do_recover(coin):
    d = load(reject_legacy=LIVE)
    validate_state(d)
    p = d[coin]
    if LIVE:
        ex = session()['ex']
        resolve(ex, d, p)
        startup(ex, d)
    print(coin, '已核对；不会补开仓。需要退出时执行 close。', p['phase'])


def do_status():
    d = load()
    if not d:
        print('无本地记录（不代表账户无仓）')
    ex = exchange() if LIVE else None
    if LIVE and not d:
        for pos in ex.fetch_positions():
            if number(pos['contracts']):
                print('账户未归属仓位：', pos['symbol'], pos['side'], pos['contracts'])
        balance = ex.fetch_balance()
        print('账户非零余额：', {coin: qty for coin, qty in balance['total'].items() if qty})
    for coin, p in d.items():
        print('交易 ID', p.get('trade_id'), '执行状态', p.get('execution_state', 'LEGACY_UNVERSIONED'),
              '目标', p.get('targets'), '确认余量', p.get('confirmed_remaining'),
              '基础币敞口', p.get('exposure_base'), '原因', p.get('decision_reason'))
        print(coin, p['phase'], '现货', p['base'], '空单', p['contracts'],
              '待确认订单', p.get('pending', {}).get('client_id'))
        if LIVE:
            print('账户现货/空单：', account(ex, p))
            if not p.get('pending'):
                reconcile(ex, p)


def main():
    global LIVE
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--live', action='store_true')
    parser.add_argument('command', nargs='?', default='status',
                        choices=['candidates', 'status', 'startup', 'open', 'close', 'recover'])
    parser.add_argument('coin', nargs='?')
    parser.add_argument('notional', type=float, nargs='?')
    args = parser.parse_args()
    LIVE = args.live
    if args.command == 'candidates':
        for r in mode_a_candidates(fetch('okx')):
            print(r['coin'], r['symbol'], '扣费名义年化估算', f"{r['apr_a_net']:.2%}")
        return
    if args.command in ('open', 'close', 'recover') and not args.coin:
        parser.error('需要币名')
    if args.command == 'open' and args.notional is None:
        parser.error('open 需要名义金额')
    print('实盘' if LIVE else '空跑')
    if args.command == 'open':
        do_open(args.coin.upper(), args.notional)
    elif args.command == 'close':
        do_close(args.coin.upper())
    elif args.command == 'recover':
        do_recover(args.coin.upper())
    elif args.command == 'startup':
        do_startup()
    else:
        with state_path().with_suffix('.lock').open('a') as lock:
            fcntl.flock(lock, fcntl.LOCK_SH)
            do_status()


if __name__ == '__main__':
    main()
