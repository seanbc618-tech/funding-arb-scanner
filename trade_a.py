"""OKX 模式 A：默认空跑，recover 仅查询原单，close 按确认余量退出。"""
import argparse
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


def state_path():
    return ROOT / ('positions_a_live.json' if LIVE else 'positions_a_dry.json')


def load():
    legacy = ROOT / 'positions_a.json'
    if legacy.exists() and json.loads(legacy.read_text()):
        raise RuntimeError('旧 positions_a.json 有记录，须人工核对后迁移；不会自动转换')
    path = state_path()
    d = json.loads(path.read_text()) if path.exists() else {}
    if any(p.get('live') is not LIVE for p in d.values()):
        raise RuntimeError('状态模式不匹配')
    return d


def save(d):
    path = state_path()
    tmp = path.with_suffix('.tmp')
    with tmp.open('w') as f:
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
    for pos in ex.fetch_positions([p['perp']]):
        n = number(pos['contracts'])
        if n and (pos.get('side') != 'short' or pos.get('marginMode') != 'cross'):
            raise RuntimeError('发现非 cross 空头仓位，停止')
        short += n
    balance = ex.fetch_balance()
    coin = p['spot'].split('/')[0]
    total = number(balance.get(coin, {}).get('total') or 0)
    if ex.fetch_open_orders(p['spot']) or ex.fetch_open_orders(p['perp']):
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
    o = p.get('pending')
    if not o:
        return
    result = ex.fetch_order(None, o['symbol'], {'clOrdId': o['client_id']})
    if result.get('status') not in ('closed', 'canceled', 'expired', 'rejected'):
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
    p[o['leg']] = max(0, remaining)
    p['orders'].append({**o, 'result': {k: result.get(k) for k in
                        ('id', 'status', 'filled', 'average', 'cost', 'fee', 'fees', 'timestamp')}})
    del p['pending']
    save(d)


def order(ex, d, p, leg, side, qty):
    if p.get('pending'):
        raise RuntimeError('存在待确认订单，请先 recover')
    sym = p['spot'] if leg == 'base' else p['perp']
    qty = amount(ex, sym, qty)
    if not LIVE:
        p[leg] += qty * (1 if (leg == 'base') == (side == 'buy') else -1)
        p[leg] = max(0, p[leg])
        p['orders'].append({'symbol': sym, 'side': side, 'amount': qty, 'simulated': True})
        save(d)
        return
    o = {'client_id': uuid.uuid4().hex, 'symbol': sym, 'side': side,
         'leg': leg, 'amount': qty, 'submitted_at': time.time()}
    p['pending'] = o
    save(d)
    params = {'clOrdId': o['client_id']}
    params.update({'tdMode': 'cash', 'tgtCcy': 'base_ccy', 'banAmend': True}
                  if leg == 'base' else
                  {'tdMode': 'cross', 'posSide': 'net', 'reduceOnly': side == 'buy'})
    try:
        ex.create_order(sym, 'market', side, qty, None, params)
    except Exception:
        # 响应异常也只查询原单；不重发、不盲目回滚。
        resolve(ex, d, p)
        return
    resolve(ex, d, p)


def do_open(coin, notional):
    notional = number(notional)
    if notional <= 0:
        raise ValueError('名义金额必须为正')
    d = load()
    if coin in d and d[coin]['phase'] != 'closed':
        raise RuntimeError('已有记录，请 recover / close')
    ex = exchange()
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
    p = {'live': LIVE, 'spot': spot, 'perp': perp, 'base': 0., 'contracts': 0.,
         'size': size, 'baseline': 0., 'phase': 'opening', 'orders': [],
         'notional': notional, 'entry_px': px, 'opened_at': time.time()}
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
        save(d)
        return
    if LIVE:
        reconcile(ex, p)
    contracts = amount(ex, perp, p['base'] / size)
    order(ex, d, p, 'contracts', 'sell', contracts)
    if LIVE:
        reconcile(ex, p)
    p['phase'] = ('open' if p['contracts'] > 0
                  and abs(p['base'] - p['contracts'] * size) <= p['base'] * .02 else 'needs_close')
    save(d)
    print(coin, p['phase'], '现货', p['base'], '空单张数', p['contracts'])


def do_close(coin):
    d = load()
    p = d[coin]
    if p.get('pending'):
        raise RuntimeError('先运行 recover 确认原订单')
    ex = exchange()
    if LIVE:
        reconcile(ex, p)
    p['phase'] = 'closing'
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
    save(d)
    print(coin, '已平仓；成交记录保留')


def do_recover(coin):
    d = load()
    p = d[coin]
    if LIVE:
        ex = exchange()
        resolve(ex, d, p)
        reconcile(ex, p)
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
                        choices=['candidates', 'status', 'open', 'close', 'recover'])
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
    with state_path().with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        if args.command == 'open':
            do_open(args.coin.upper(), args.notional)
        elif args.command == 'close':
            do_close(args.coin.upper())
        elif args.command == 'recover':
            do_recover(args.coin.upper())
        else:
            do_status()


if __name__ == '__main__':
    main()
