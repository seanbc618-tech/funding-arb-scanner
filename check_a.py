"""模式 A 只读风险/盘口检查；阈值是操作限制，不是收益承诺。"""
import argparse
import json
import math
import time

MAX_SLIPPAGE_BPS = 20
MAX_BASIS_BPS = 100
MIN_LIQ_DISTANCE = .10
MIN_MARGIN_RATIO = 3.0  # OKX mgnRatio 原始倍数；3 = 300%
MAX_EXPOSURE = .02
MAX_BOOK_AGE_MS = 5000
MAX_SNAPSHOT_MS = 10000


def num(x):
    n = float(x)
    if not math.isfinite(n):
        raise ValueError('NONFINITE_DATA')
    return n


def execution(ex, symbol, side, qty, now_ms=None):
    """订单簿数量与 ccxt 下单数量一致：现货币量，永续张数。"""
    qty = num(qty)
    if qty <= 0:
        raise ValueError('NONPOSITIVE_QUANTITY')
    book = ex.fetch_order_book(symbol, limit=50)
    now = int(time.time() * 1000) if now_ms is None else now_ms
    ts = num(book['timestamp'])
    if not 0 <= now - ts <= MAX_BOOK_AGE_MS:
        raise ValueError('BOOK_STALE_OR_FUTURE')
    asks, bids = book['asks'], book['bids']
    if not asks or not bids or num(bids[0][0]) >= num(asks[0][0]):
        raise ValueError('BOOK_EMPTY_OR_CROSSED')
    levels = asks if side == 'buy' else bids
    best = num(levels[0][0])
    if best <= 0:
        raise ValueError('INVALID_PRICE')
    raw_limit = best * (1 + MAX_SLIPPAGE_BPS / 10000 * (1 if side == 'buy' else -1))
    price = num(ex.price_to_precision(symbol, raw_limit))
    # 不允许精度舍入把限价推到风险上限之外。
    if (side == 'buy' and price > raw_limit) or (side == 'sell' and price < raw_limit):
        price = num(ex.price_to_precision(symbol, best))
    left, cost, previous = qty, 0., best
    for level in levels:
        px, size = num(level[0]), num(level[1])
        if px <= 0 or size < 0 or (side == 'buy' and px < previous) or (side == 'sell' and px > previous):
            raise ValueError('INVALID_BOOK_LEVEL')
        previous = px
        if (side == 'buy' and px > price) or (side == 'sell' and px < price):
            break
        take = min(left, size)
        cost += take * px
        left -= take
        if left <= qty * 1e-10:
            break
    if left > qty * 1e-10:
        raise ValueError('INSUFFICIENT_DEPTH_WITHIN_PRICE_LIMIT')
    return {'symbol': symbol, 'side': side, 'amount': qty, 'limit_price': price,
            'vwap': cost / qty, 'best': best, 'book_timestamp': ts,
            'slippage_bps': abs(cost / qty / best - 1) * 10000}


def check(ex, p, opening=False, private=True):
    start = time.time()
    report = {'action': 'BLOCK', 'reasons': [], 'metrics': {}, 'gaps': [], 'checked_at': start}
    try:
        base, contracts, size = num(p['base']), num(p['contracts']), num(p['size'])
        if min(base, contracts) < 0 or size <= 0:
            raise ValueError('INVALID_POSITION_SIZE')
        if p.get('pending'):
            raise ValueError('PENDING_ORDER_RECOVER_FIRST')
        if not opening and p.get('phase') == 'closed':
            report['action'] = 'CLOSED'
            return report
        spot = execution(ex, p['spot'], 'buy' if opening else 'sell', base) if base else None
        perp = execution(ex, p['perp'], 'sell' if opening else 'buy', contracts) if contracts else None
        report['metrics']['spot_execution'] = spot
        report['metrics']['perp_execution'] = perp
        reference = spot['vwap'] if spot else (perp['vwap'] if perp else 0)
        if reference <= 0:
            raise ValueError('NO_POSITION')
        if spot and perp:
            basis = (perp['vwap'] / spot['vwap'] - 1) * 10000
            report['metrics']['basis_bps'] = basis
            if opening and abs(basis) > MAX_BASIS_BPS:
                report['reasons'].append('ENTRY_BASIS_TOO_LARGE')
        exposure = abs(base - contracts * size) / max(base, contracts * size)
        report['metrics']['exposure_fraction'] = exposure
        if exposure > MAX_EXPOSURE:
            report['reasons'].append('EXPOSURE_LIMIT')
        funding = ex.fetch_funding_rate(p['perp'])
        rate = num(funding['fundingRate'])
        next_ts = num(funding['fundingTimestamp'])
        if next_ts <= time.time() * 1000 or next_ts - time.time() * 1000 > 24 * 3600000:
            raise ValueError('FUNDING_CLOCK_INVALID')
        report['metrics']['funding_rate'] = rate
        if rate < 0:
            report['reasons'].append('FUNDING_NEGATIVE')
        if private:
            import trade_a
            if opening:
                _, held = trade_a.account(ex, p)
                if held:
                    raise ValueError('EXISTING_PERP_POSITION')
            else:
                trade_a.reconcile(ex, p)
            balance = ex.fetch_balance()
            if opening:
                free = num(balance['USDT']['free'])
                # 按 1x 永续预留 + 现货 + 10% 缓冲；不改变账户实际杠杆。
                required = (base * spot['limit_price'] + contracts * size * perp['limit_price']) * 1.1
                report['metrics'].update(free_usdt=free, required_usdt=required)
                if free < required:
                    report['reasons'].append('INSUFFICIENT_FREE_USDT')
            positions = [x for x in ex.fetch_positions() if num(x['contracts']) > 0]
            if positions:
                ratio = num(balance['info']['data'][0]['mgnRatio'])
                report['metrics']['margin_ratio'] = ratio
                if ratio <= MIN_MARGIN_RATIO:
                    report['reasons'].append('MARGIN_RATIO_LOW')
            for pos in positions:
                if pos['symbol'] != p['perp']:
                    continue
                mark, liq = num(pos['markPrice']), num(pos['liquidationPrice'])
                if mark <= 0 or liq <= 0:
                    raise ValueError('LIQUIDATION_PRICE_UNKNOWN')
                distance = (liq - mark) / mark
                report['metrics']['liquidation_distance'] = distance
                if distance <= MIN_LIQ_DISTANCE:
                    report['reasons'].append('LIQUIDATION_DISTANCE_LOW')
        report['action'] = ('BLOCK' if opening else 'EXIT') if report['reasons'] else ('PASS' if opening else 'HOLD')
        if not opening and p.get('phase') in ('opening', 'closing', 'needs_close'):
            report['action'] = 'EXIT'
            report['reasons'].append('INCOMPLETE_EXECUTION')
        if time.time() - start > MAX_SNAPSHOT_MS / 1000:
            raise ValueError('SNAPSHOT_TOO_SLOW')
    except Exception as exc:
        report['action'] = 'BLOCK'
        report['gaps'].append(f'{type(exc).__name__}:{exc}')
    return report


def main():
    import trade_a
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('coin')
    parser.add_argument('--notional', type=float, help='检查计划开仓；省略时检查本地持仓')
    parser.add_argument('--account', action='store_true', help='只读查询真实账户及实盘状态')
    args = parser.parse_args()
    trade_a.LIVE = args.account
    ex = trade_a.exchange()
    coin = args.coin.upper()
    if args.notional is None:
        p = trade_a.load()[coin]
    else:
        p = trade_a.plan(ex, coin, args.notional)
    print(json.dumps(check(ex, p, opening=args.notional is not None, private=args.account), ensure_ascii=False, indent=2))


if __name__ == '__main__':
    main()
