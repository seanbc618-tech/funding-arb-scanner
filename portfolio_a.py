"""F05 只读收益与资金展示；不采集写库、不下单、不自动归属外部持仓。"""
import argparse
from contextlib import closing
import hashlib
import json
from pathlib import Path
import sqlite3
import time

from accounting import dec, covered, summarize
from check_a import execution, MAX_BOOK_AGE_MS, MAX_SNAPSHOT_MS


def optional(value):
    try:
        return str(dec(value))
    except (ValueError, ArithmeticError):
        return None


def read_evidence(path, account, p, markets, now):
    """必须使用只读 SQLite；旧库无覆盖表时报告缺口，不修改 schema。"""
    fills, bills, gaps = [], [], []
    start = int(p['opened_at'] * 1000)
    end = int(p.get('closed_at', now / 1000) * 1000)
    if not 0 <= start <= end <= now:
        return [], [], ['INVALID_EVIDENCE_WINDOW']
    if not path.is_file():
        return [], [], ['EVIDENCE_DB_MISSING']
    spot, perp = markets[p['spot']]['id'], markets[p['perp']]['id']
    with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True)) as db:
        db.execute('PRAGMA query_only=ON')
        for kind, scope in [('fills', spot), ('fills', perp), ('bills', 'ACCOUNT')]:
            if not covered(db, account, kind, scope, start, end,
                           ('ALL:SPOT' if scope == spot else 'ALL:SWAP') if kind == 'fills' else None):
                gaps.append('QUERY_COVERAGE_MISSING:' + kind)
        if db.execute('SELECT 1 FROM evidence_conflicts WHERE account=? LIMIT 1', (account,)).fetchone():
            gaps.append('EVIDENCE_CONFLICT_REQUIRES_REVIEW')
        for kind, raw in db.execute('SELECT kind,raw FROM evidence WHERE account=?', (account,)):
            row = json.loads(raw)
            if not start <= int(row['ts']) <= end:
                continue
            if kind == 'fills' and row.get('instId') in (spot, perp):
                order = next((o for o in p['orders'] if str(o.get('result', {}).get('id')) == str(row.get('ordId'))), None)
                if order and row['instId'] != (spot if order.get('leg') == 'base' else perp):
                    gaps.append('FILL_INSTRUMENT_MISMATCH')
                fills.append(row)
            if kind == 'bills' and (row.get('instId') in (spot, perp) or
                                   (not row.get('instId') and row['type'] != '1')):
                bills.append(row)
    return fills, bills, gaps


def pnl(p, fills, bills, now, evidence_gaps=()):
    """已确认成交按移动加权成本分拆；费用独立，不重复计费。"""
    result = {'booked_funding_usdt': None, 'fees_by_currency': None,
              'realized_trade_pnl_usdt': None, 'remaining_spot_cost_usdt': None,
              'remaining_short_entry_notional_usdt': None, 'unrealized_pnl_usdt': None,
              'estimated_net_pnl_usdt': None, 'settled_net_pnl_usdt': None,
              'gaps': list(evidence_gaps)}
    if p.get('live') is not True:
        result['gaps'].append('SIMULATED_POSITION'); return result
    if evidence_gaps:
        return result
    # 复用订单数量/方向/费用/账单归属校验，仅移除未平仓估值不适用的检查。
    validation = summarize(p, fills, bills, now)
    exempt = {'POSITION_NOT_FLAT', 'FILL_INVENTORY_NOT_FLAT', 'ARCHIVE_SETTLEMENT_WAIT_1H'}
    result['gaps'].extend(r for r in validation['reasons'] if r not in exempt)
    if result['gaps']:
        return result
    known = {str(o['result']['id']): o for o in p['orders'] if o.get('result', {}).get('id')}
    qty = {'base': dec(0), 'contracts': dec(0)}
    cost = {'base': dec(0), 'contracts': dec(0)}
    realized = dec(0)
    for fill in sorted(fills, key=lambda f: (int(f['ts']), int(f['billId']))):
        order = known[str(fill['ordId'])]; leg = order['leg']
        if leg not in qty:
            raise ValueError('UNKNOWN_ORDER_LEG')
        amount, price = dec(fill['fillSz']), dec(fill['fillPx'])
        unit = dec(p['size']) if leg == 'contracts' else dec(1)
        entering = fill['side'] == ('buy' if leg == 'base' else 'sell')
        base_fee = -dec(fill['fee']) if leg == 'base' and fill['feeCcy'] == p['spot'].split('/')[0] else dec(0)
        if entering:
            if amount - base_fee <= 0: raise ValueError('FEE_EXCEEDS_FILL')
            qty[leg] += amount - base_fee; cost[leg] += amount * unit * price
        else:
            consumed = amount + base_fee
            if qty[leg] <= 0 or consumed <= 0 or consumed > qty[leg]:
                raise ValueError('FILL_INVENTORY_UNDERFLOW')
            removed = cost[leg] * consumed / qty[leg]
            realized += (amount * unit * price - removed) * (1 if leg == 'base' else -1)
            cost[leg] -= removed; qty[leg] -= consumed
    # 现货币量费用通过净数量与成本体现，不再次按 USDT 费用扣除。
    result['booked_funding_usdt'] = validation['funding_usdt']
    result['fees_by_currency'] = validation['fees_by_currency']
    if any(abs(qty[k] - dec(p[k])) > dec('1e-10') for k in qty):
        result['gaps'].append('CONFIRMED_REMAINDER_MISMATCH'); return result
    result.update(realized_trade_pnl_usdt=str(realized), remaining_spot_cost_usdt=str(cost['base']),
                  remaining_short_entry_notional_usdt=str(cost['contracts']))
    if p['phase'] == 'closed' and qty['base'] == 0 and qty['contracts'] == 0:
        result['unrealized_pnl_usdt'] = '0'
        result['settled_net_pnl_usdt'] = validation['net_pnl_usdt']
        if validation['net_pnl_usdt'] is None:
            result['gaps'].append('ARCHIVE_SETTLEMENT_WAIT_1H')
    return result


def value_position(ex, p, ledger):
    result = dict(ledger)
    result['gaps'] = list(ledger['gaps'])
    result.update(exit_fee_estimate_usdt=None, exit_spread_slippage_usdt=None,
                  exit_total_cost_usdt=None, estimated_net_after_exit_usdt=None,
                  spot_market_value_usdt=None, valuation_basis='fresh_order_book_mid', quotes={})
    if p.get('phase') == 'closed' and dec(p['base']) == 0 and dec(p['contracts']) == 0:
        return result | {'exit_fee_estimate_usdt': '0', 'exit_spread_slippage_usdt': '0',
                         'exit_total_cost_usdt': '0', 'spot_market_value_usdt': '0'}
    quotes, fees = {}, {}
    for key, symbol, side in [('base', p['spot'], 'sell'), ('contracts', p['perp'], 'buy')]:
        amount = dec(p[key])
        if amount == 0:
            continue
        try:
            quotes[key] = execution(ex, symbol, side, float(amount))
        except Exception as exc:
            result['gaps'].append('EXIT_QUOTE_UNKNOWN:' + key + ':' + type(exc).__name__)
        try:
            fees[key] = dec(ex.fetch_trading_fee(symbol)['taker'])  # ccxt 已把 OKX 费用符号转换。
        except Exception as exc:
            result['gaps'].append('EXIT_FEE_UNKNOWN:' + key + ':' + type(exc).__name__)
    now = time.time() * 1000
    for key, quote in list(quotes.items()):
        if not 0 <= now - quote['book_timestamp'] <= MAX_BOOK_AGE_MS:
            result['gaps'].append('EXIT_QUOTE_STALE:' + key); del quotes[key]
    needed = {key for key in ('base', 'contracts') if dec(p[key]) > 0}
    result['quotes'] = quotes
    if not needed <= quotes.keys():
        return result
    spot_value = dec(p['base']) * dec(quotes['base']['mid_price']) if 'base' in needed else dec(0)
    short_value = dec(p['contracts']) * dec(p['size']) * dec(quotes['contracts']['mid_price']) if 'contracts' in needed else dec(0)
    result['spot_market_value_usdt'] = str(spot_value)
    if ledger['realized_trade_pnl_usdt'] is not None:
        unrealized = spot_value - dec(ledger['remaining_spot_cost_usdt']) + dec(ledger['remaining_short_entry_notional_usdt']) - short_value
        net = dec(ledger['realized_trade_pnl_usdt']) + unrealized + dec(ledger['booked_funding_usdt']) - dec((ledger['fees_by_currency'] or {}).get('USDT', 0))
        result.update(unrealized_pnl_usdt=str(unrealized), estimated_net_pnl_usdt=str(net))
    spread = sum((dec(p[key]) * (dec(p['size']) if key == 'contracts' else dec(1)) *
                  (dec(quotes[key]['vwap']) - dec(quotes[key]['mid_price'])) *
                  (1 if key == 'contracts' else -1) for key in needed), dec(0))
    result['exit_spread_slippage_usdt'] = str(spread)
    if needed <= fees.keys():
        fee = sum((dec(p[key]) * (dec(p['size']) if key == 'contracts' else dec(1)) *
                   dec(quotes[key]['vwap']) * fees[key] for key in needed), dec(0))
        result.update(exit_fee_estimate_usdt=str(fee), exit_total_cost_usdt=str(spread + fee))
        if result['estimated_net_pnl_usdt'] is not None:
            result['estimated_net_after_exit_usdt'] = str(dec(result['estimated_net_pnl_usdt']) - spread - fee)
    return result


def report(ex, local, db_path):
    started = time.monotonic(); now = int(time.time() * 1000)
    output = {'checked_at_ms': now, 'account': {}, 'positions': {}, 'gaps': [],
              'strategy_nav_usdt': None, 'capital_return': None,
              'capital_gap': 'STRATEGY_CAPITAL_ALLOCATION_UNKNOWN'}
    account_id = None
    balance = None
    try:
        config = ex.private_get_account_config()['data'][0]
        if not config.get('uid'): raise ValueError('ACCOUNT_ID_UNKNOWN')
        account_id = hashlib.sha256(str(config['uid']).encode()).hexdigest()
        output['account'].update(acctLv=config.get('acctLv'), posMode=config.get('posMode'))
    except Exception as exc:
        output['gaps'].append('ACCOUNT_CONFIG_UNKNOWN:' + type(exc).__name__)
    try:
        balance = ex.fetch_balance(); raw = balance['info']['data'][0]
        output['account'].update(equity_usd=optional(raw.get('totalEq')),
                                cross_initial_margin_usd=optional(raw.get('imr')),
                                isolated_equity_usd=optional(raw.get('isoEq')),
                                free_usdt=optional(balance.get('USDT', {}).get('free')))
        if output['account']['equity_usd'] is None: output['gaps'].append('ACCOUNT_EQUITY_UNKNOWN')
    except Exception as exc:
        output['gaps'].append('ACCOUNT_BALANCE_UNKNOWN:' + type(exc).__name__)
    observed = None
    try:
        observed = ex.fetch_positions()
        if not isinstance(observed, list): raise ValueError('INVALID_POSITIONS_RESPONSE')
        output['account']['positions'] = [{k: row.get(k) for k in
            ('symbol', 'side', 'contracts', 'marginMode', 'unrealizedPnl')} for row in observed if dec(row['contracts']) != 0]
    except Exception as exc:
        observed = None
        output['gaps'].append('ACCOUNT_POSITIONS_UNKNOWN:' + type(exc).__name__)
    for coin, p in local.items():
        try:
            fills, bills, gaps = read_evidence(Path(db_path), account_id, p, ex.markets, now) if account_id else ([], [], ['ACCOUNT_ID_UNKNOWN'])
            ledger = pnl(p, fills, bills, now, gaps)
            if p.get('phase') != 'closed':
                try:
                    import trade_a
                    trade_a.reconcile(ex, p)  # 仅查询；未知余额/挂单/方向不能作为当前估值依据。
                except Exception as exc:
                    ledger['gaps'].append('ACCOUNT_RECONCILIATION_FAILED:' + type(exc).__name__)
                    output['positions'][coin] = ledger
                    continue
            item = value_position(ex, p, ledger)
            item['perp_initial_margin_usd'] = None
            matched = [x for x in (observed or []) if x.get('symbol') == p['perp'] and dec(x['contracts']) != 0]
            if observed is not None and dec(p['contracts']) == 0 and not matched:
                item['perp_initial_margin_usd'] = '0'
            elif len(matched) == 1 and matched[0].get('side') == 'short' and dec(matched[0]['contracts']) == dec(p['contracts']):
                item['perp_initial_margin_usd'] = optional(matched[0].get('info', {}).get('imr'))
            if item['perp_initial_margin_usd'] is None: item['gaps'].append('POSITION_MARGIN_ALLOCATION_UNKNOWN')
            output['positions'][coin] = item
        except Exception as exc:
            output['positions'][coin] = {'gaps': ['POSITION_REPORT_ERROR:' + type(exc).__name__]}
    symbols = {p['perp'] for p in local.values() if p.get('phase') != 'closed'}
    output['account']['unowned_position_count'] = (sum(1 for p in output['account'].get('positions', []) if p['symbol'] not in symbols)
                                                    if 'positions' in output['account'] else None)
    if output['account']['unowned_position_count']: output['gaps'].append('ACCOUNT_POSITION_UNOWNED')
    completed_ms = time.time() * 1000
    for item in output['positions'].values():
        if any(not 0 <= completed_ms - q['book_timestamp'] <= MAX_BOOK_AGE_MS
               for q in item.get('quotes', {}).values()):
            item['gaps'].append('VALUATION_QUOTES_STALE_AT_COMPLETION')
            for key in ('estimated_net_pnl_usdt', 'estimated_net_after_exit_usdt', 'unrealized_pnl_usdt',
                        'exit_total_cost_usdt', 'exit_fee_estimate_usdt', 'exit_spread_slippage_usdt',
                        'spot_market_value_usdt'):
                item[key] = None
    output['elapsed_ms'] = (time.monotonic() - started) * 1000
    if output['elapsed_ms'] > MAX_SNAPSHOT_MS:
        output['gaps'].append('SNAPSHOT_TOO_SLOW')
        for item in output['positions'].values():
            for key in ('estimated_net_pnl_usdt', 'estimated_net_after_exit_usdt', 'unrealized_pnl_usdt', 'exit_total_cost_usdt',
                        'exit_fee_estimate_usdt', 'exit_spread_slippage_usdt', 'spot_market_value_usdt',
                        'perp_initial_margin_usd'):
                item[key] = None
    output['status'] = ('DATA_GAP' if output['gaps'] or any(p['gaps'] for p in output['positions'].values()) else
                        'NO_STRATEGY_POSITIONS' if not local else 'AS_OF_QUERY')
    return output


def markdown(data):
    lines = ['# 持仓收益与资金（只读）', '', f'状态：{data["status"]}', '',
             '| 账户字段 | 数值 |', '|---|---|']
    for key in ('equity_usd', 'cross_initial_margin_usd', 'isolated_equity_usd', 'free_usdt', 'posMode'):
        lines.append(f'| {key} | {data["account"].get(key)} |')
    lines += ['', '实际账户仓位（不自动归属或接管）：', '',
              '| 合约 | 方向 | 张数 | 保证金模式 | 交易所未实现损益（原接口口径） |', '|---|---|---|---|---|']
    for pos in data['account'].get('positions', []):
        lines.append('| ' + ' | '.join(str(pos.get(k)) for k in
            ('symbol','side','contracts','marginMode','unrealizedPnl')) + ' |')
    lines += ['', '账户净资产包含外部持仓；不是策略净值。策略资金分配未知，净值与收益率为 null。', '',
              '| 标的 | 已入账资金费 USDT | 已实现交易损益 USDT | 未实现 USDT | 预计退出成本 USDT | 退出后估计净损益 USDT |', '|---|---|---|---|---|---|']
    for coin, item in data['positions'].items():
        lines.append('| ' + ' | '.join(str(v) for v in [coin, *[item.get(k) for k in
            ('booked_funding_usdt','realized_trade_pnl_usdt','unrealized_pnl_usdt','exit_total_cost_usdt','estimated_net_after_exit_usdt')]]) + ' |')
        lines += ['', f'{coin} 手续费原币：{item.get("fees_by_currency")}；现货剩余成本 USDT：{item.get("remaining_spot_cost_usdt")}；永续保证金 USD：{item.get("perp_initial_margin_usd")}；已平仓净损益：{item.get("settled_net_pnl_usdt")}',
                  f'缺口：{item["gaps"]}', '']
    lines += ['', '全局缺口：' + str(data['gaps']), '未平仓数字为盘口估值，预计退出费用未入账；未提交任何订单。']
    return '\n'.join(lines)


def main():
    import trade_a
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--db', type=Path, default=trade_a.ROOT / 'accounting_a.sqlite')
    parser.add_argument('--markdown', action='store_true')
    args = parser.parse_args()
    trade_a.LIVE = True
    local = trade_a.load()
    data = report(trade_a.exchange(), local, args.db)
    print(markdown(data) if args.markdown else json.dumps(data, ensure_ascii=False, indent=2))


if __name__ == '__main__': main()
