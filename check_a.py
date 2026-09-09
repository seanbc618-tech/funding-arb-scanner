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
            'mid_price': (num(asks[0][0]) + num(bids[0][0])) / 2,
            'slippage_bps': abs(cost / qty / best - 1) * 10000}


def check(ex, p, opening=False, private=True):
    start, started = time.time(), time.monotonic()
    report = {'action': 'BLOCK', 'reasons': [], 'metrics': {}, 'gaps': [], 'checked_at': start,
              'evidence_at': {}, 'data_status': 'INCOMPLETE',
              'risk': {'status': 'UNKNOWN', 'reasons': [], 'gaps': [],
                       'scope': 'account_and_local' if private else 'local_and_public'},
              'execution': {'status': 'BLOCKED', 'limitations': []}}
    metrics, risk, executable = report['metrics'], report['risk'], report['execution']

    def gap(source, exc, affects_risk=True):
        message = f'{source}:{type(exc).__name__}:{exc}'
        report['gaps'].append(message)
        executable['limitations'].append(message)
        if affects_risk:
            risk['gaps'].append(message)

    def reason(code, affects_risk=True):
        report['reasons'].append(code)
        if affects_risk:
            risk['reasons'].append(code)
        else:
            executable['limitations'].append(code)

    # 各腿数量独立读取：一腿未知不妨碍采集另一腿盘口和账户风险。
    quantities = {}
    for key in ('base', 'contracts', 'size'):
        try:
            value = num(p[key])
            if value < 0 or (key == 'size' and value == 0):
                raise ValueError('INVALID_POSITION_SIZE')
            quantities[key] = value
        except Exception as exc:
            gap('local_' + key, exc)
    if p.get('pending'):
        gap('local_state', ValueError('PENDING_ORDER_RECOVER_FIRST'))
    closed = (not opening and p.get('phase') == 'closed'
              and quantities.get('base') == 0 and quantities.get('contracts') == 0
              and not p.get('pending') and not report['gaps'])
    if closed:
        report.update(action='CLOSED', data_status='COMPLETE', completed_at=time.time(),
                      elapsed_ms=(time.monotonic() - started) * 1000)
        risk['status'] = 'CLOSED'
        risk['scope'] = 'local_closed_record'  # 历史状态，不代表账户空仓。
        executable['status'] = 'NOT_REQUIRED'
        return report
    if p.get('phase') not in ('opening', 'open', 'closing', 'needs_close', 'closed'):
        gap('local_state', ValueError('LOCAL_PHASE_UNKNOWN'))
    if not opening and p.get('phase') == 'closed':
        gap('local_state', ValueError('CLOSED_STATE_INCONSISTENT'))
    if not opening and p.get('phase') in ('opening', 'closing', 'needs_close'):
        reason('INCOMPLETE_EXECUTION')
    if len(quantities) == 3:
        base, contracts, size = (quantities[k] for k in ('base', 'contracts', 'size'))
        denominator = max(base, contracts * size)
        if denominator == 0:
            gap('local_exposure', ValueError('NO_POSITION'))
        else:
            exposure = abs(base - contracts * size) / denominator
            metrics['exposure_fraction'] = exposure
            if exposure > MAX_EXPOSURE:
                reason('EXPOSURE_LIMIT')
    report['evidence_at']['local_state'] = time.time()

    # 先采集私有风险，任何一个接口或单行失败都不能吞掉其他已知风险。
    balance, positions = None, None
    if private:
        try:
            balance = ex.fetch_balance()
            if not isinstance(balance, dict):
                raise ValueError('INVALID_BALANCE_RESPONSE')
            report['evidence_at']['account_balance'] = time.time()
        except Exception as exc:
            gap('account_balance', exc)
        try:
            positions = ex.fetch_positions()
            if not isinstance(positions, list):
                positions = None
                raise ValueError('INVALID_POSITIONS_RESPONSE')
            report['evidence_at']['account_positions'] = time.time()
        except Exception as exc:
            gap('account_positions', exc)
        active = []
        for index, pos in enumerate(positions or []):
            try:
                held = num(pos['contracts'])
                if held < 0:
                    raise ValueError('NEGATIVE_CONTRACTS')
                if held == 0:
                    continue
            except Exception as exc:
                gap(f'account_position_{index}', exc)
                # 未知张数的字典行仍可提供标记价、方向和清算价证据。
                if not isinstance(pos, dict):
                    continue
            active.append(pos)
        position_data_unknown = positions is None or any(
            item.startswith('account_position_') for item in report['gaps'])
        if balance is not None and (active or position_data_unknown):
            try:
                ratio = num(balance['info']['data'][0]['mgnRatio'])
                metrics['margin_ratio'] = ratio
                if ratio <= MIN_MARGIN_RATIO:
                    reason('MARGIN_RATIO_LOW')
            except Exception as exc:
                gap('margin_ratio', exc)
        target_positions = [pos for pos in active if pos.get('symbol') == p.get('perp')]
        if len(target_positions) > 1:
            gap('account_positions', ValueError('DUPLICATE_TARGET_POSITIONS'))
        distances = []
        for index, pos in enumerate(target_positions):
            side = pos.get('side')
            if side in ('long', 'short') and side != 'short':
                reason('POSITION_SIDE_MISMATCH')
            try:
                if side not in ('long', 'short'):
                    raise ValueError('POSITION_SIDE_UNKNOWN')
                mark, liq = num(pos['markPrice']), num(pos['liquidationPrice'])
                if mark <= 0 or liq <= 0:
                    raise ValueError('LIQUIDATION_PRICE_UNKNOWN')
                distance = (liq - mark) / mark if side == 'short' else (mark - liq) / mark
                distances.append(distance)
                if distance <= MIN_LIQ_DISTANCE:
                    reason('LIQUIDATION_DISTANCE_LOW')
            except Exception as exc:
                gap(f'liquidation_{index}', exc)
        if distances:
            metrics['liquidation_distance'] = min(distances)
        # 复用执行器已有账户模式、订单、方向及确认余量门槛；失败仍保留上面的风险。
        try:
            import trade_a
            if opening:
                _, held = trade_a.account(ex, p)
                if held:
                    raise ValueError('EXISTING_PERP_POSITION')
            else:
                trade_a.reconcile(ex, p)
            report['evidence_at']['account_reconciliation'] = time.time()
        except Exception as exc:
            gap('account_reconciliation', exc)

    try:
        funding = ex.fetch_funding_rate(p['perp'])
        rate = num(funding['fundingRate'])
        next_ts = num(funding['fundingTimestamp'])
        now_ms = time.time() * 1000
        if not 0 < next_ts - now_ms <= 24 * 3600000:
            raise ValueError('FUNDING_CLOCK_INVALID')
        metrics.update(funding_rate=rate, funding_timestamp=next_ts)
        report['evidence_at']['funding'] = time.time()
        if rate < 0:
            reason('FUNDING_NEGATIVE')
    except Exception as exc:
        gap('funding', exc)

    # 两个盘口分别尝试，盘口失败只限制执行，不抹去风险结论。
    for name, key, side in (('spot', 'base', 'buy' if opening else 'sell'),
                            ('perp', 'contracts', 'sell' if opening else 'buy')):
        metrics[name + '_execution'] = None
        qty = quantities.get(key)
        if qty is None or qty == 0:
            continue
        try:
            metrics[name + '_execution'] = execution(ex, p[name], side, qty)
            report['evidence_at'][name + '_book'] = time.time()
        except Exception as exc:
            gap(name + '_book', exc, affects_risk=False)
    spot, perp = metrics['spot_execution'], metrics['perp_execution']
    if spot and perp:
        basis = (perp['vwap'] / spot['vwap'] - 1) * 10000
        metrics['basis_bps'] = basis
        if opening and abs(basis) > MAX_BASIS_BPS:
            reason('ENTRY_BASIS_TOO_LARGE', affects_risk=False)
    if opening and private and balance is not None:
        try:
            free = num(balance['USDT']['free'])
            metrics['free_usdt'] = free
            if not spot or not perp or 'size' not in quantities:
                raise ValueError('ENTRY_CAPITAL_ESTIMATE_UNAVAILABLE')
            # 按 1x 永续预留 + 现货 + 10% 缓冲；不改变账户实际杠杆。
            required = (quantities['base'] * spot['limit_price']
                        + quantities['contracts'] * quantities['size'] * perp['limit_price']) * 1.1
            metrics['required_usdt'] = required
            if free < required:
                reason('INSUFFICIENT_FREE_USDT', affects_risk=False)
        except Exception as exc:
            gap('entry_capital', exc, affects_risk=False)

    report['completed_at'] = time.time()
    report['elapsed_ms'] = (time.monotonic() - started) * 1000
    # 后一接口变慢时，不能继续把先前盘口视为可执行的新鲜价格。
    for name in ('spot', 'perp'):
        book = metrics[name + '_execution']
        if book and not 0 <= report['completed_at'] * 1000 - book['book_timestamp'] <= MAX_BOOK_AGE_MS:
            gap(name + '_book', ValueError('BOOK_STALE_OR_FUTURE_AT_COMPLETION'), affects_risk=False)
    if report['elapsed_ms'] > MAX_SNAPSHOT_MS:
        gap('freshness', ValueError('SNAPSHOT_TOO_SLOW'))
    for section, key in ((report, 'reasons'), (risk, 'reasons')):
        section[key] = list(dict.fromkeys(section[key]))
    report['data_status'] = 'INCOMPLETE' if report['gaps'] else 'COMPLETE'
    risk['status'] = (('ENTRY_BLOCKED' if opening else 'EXIT_REQUIRED') if risk['reasons']
                      else 'UNKNOWN' if risk['gaps'] else 'CLEAR')
    executable['status'] = 'BLOCKED' if executable['limitations'] else 'READY'
    if not executable['limitations']:
        report['action'] = (('BLOCK' if opening else 'EXIT') if report['reasons']
                            else 'PASS' if opening else 'HOLD')
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
