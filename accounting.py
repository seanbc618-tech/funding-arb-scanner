"""OKX 只读账目采集。SQLite 保存原始证据；资料不足时净损益为 null。"""
import argparse
from decimal import Decimal
import hashlib
import json
from pathlib import Path
import sqlite3
import time

DAY_MS = 86400000


def dec(value):
    n = Decimal(str(value))
    if not n.is_finite():
        raise ValueError('NONFINITE_ACCOUNTING_VALUE')
    return n


def pages(method, params):
    """OKX 原始 billId 游标；必须拉到空页，不能把页数上限当完整。"""
    rows, cursor = {}, None
    for _ in range(1000):
        response = method({**params, 'limit': '100', **({'after': cursor} if cursor else {})})
        if str(response.get('code')) != '0' or not isinstance(response.get('data'), list):
            raise ValueError('INVALID_API_RESPONSE')
        batch = response['data']
        if not batch:
            return list(rows.values())
        ids = [int(x['billId']) for x in batch]
        if cursor and max(ids) >= int(cursor):
            raise ValueError('PAGINATION_NOT_ADVANCING')
        for row in batch:
            key = str(row['billId'])
            if key in rows and rows[key] != row:
                raise ValueError('CONFLICTING_BILL_ID')
            rows[key] = row
        cursor = str(min(ids))
    raise ValueError('PAGINATION_LIMIT')


def store(db, account_id, kind, rows):
    for row in rows:
        raw = json.dumps(row, sort_keys=True, separators=(',', ':'))
        key = str(row['billId'])
        old = db.execute('SELECT raw FROM evidence WHERE account=? AND kind=? AND id=?',
                         (account_id, kind, key)).fetchone()
        if old and old[0] != raw:
            raise ValueError(f'IMMUTABLE_EVIDENCE_CONFLICT:{kind}:{key}')
        db.execute('INSERT OR IGNORE INTO evidence VALUES (?,?,?,?)', (account_id, kind, key, raw))


def summarize(p, fills, bills, now_ms):
    reasons = []
    if p.get('pending'):
        reasons.append('PENDING_ORDER')
    if p.get('phase') != 'closed' or dec(p['base']) != 0 or dec(p['contracts']) != 0:
        reasons.append('POSITION_NOT_FLAT')
    end = int(p.get('closed_at', now_ms / 1000) * 1000)
    if now_ms - end < 3600000:
        reasons.append('ARCHIVE_SETTLEMENT_WAIT_1H')
    known = {str(o['result']['id']): o for o in p['orders'] if o.get('result', {}).get('id')}
    if not known:
        reasons.append('NO_CONFIRMED_ORDERS')
    totals, order_fees = {}, {}
    cash, fees_usdt, spot_qty, short_qty = Decimal(0), Decimal(0), Decimal(0), Decimal(0)
    fee_by_ccy = {}
    base_ccy = p['spot'].split('/')[0]
    for f in fills:
        oid = str(f['ordId'])
        if oid not in known:
            reasons.append('UNATTRIBUTED_FILL')
            continue
        o = known[oid]
        if f['side'] != o['side']:
            raise ValueError('FILL_SIDE_MISMATCH')
        qty, price, fee = dec(f['fillSz']), dec(f['fillPx']), -dec(f['fee'])
        if qty <= 0 or price <= 0:
            raise ValueError('INVALID_FILL')
        totals[oid] = totals.get(oid, Decimal(0)) + qty
        ccy = f['feeCcy']
        key = (oid, ccy)
        order_fees[key] = order_fees.get(key, Decimal(0)) + fee
        fee_by_ccy[ccy] = fee_by_ccy.get(ccy, Decimal(0)) + fee
        sign = Decimal(1) if f['side'] == 'sell' else Decimal(-1)
        if o['leg'] == 'base':
            cash += sign * qty * price
            spot_qty -= sign * qty
            if ccy == base_ccy:
                spot_qty -= fee  # 已通过减少最终可卖币量体现费用，不重复从现金减
            elif ccy != 'USDT':
                reasons.append('UNPRICED_FEE_CURRENCY')
        else:
            cash += sign * qty * dec(p['size']) * price
            short_qty += sign * qty
            if ccy != 'USDT':
                reasons.append('UNPRICED_FEE_CURRENCY')
        if ccy == 'USDT':
            fees_usdt += fee
    for oid, o in known.items():
        if abs(totals.get(oid, Decimal(0)) - dec(o['result']['filled'])) > Decimal('1e-10'):
            reasons.append('FILL_TOTAL_MISMATCH')
        fees = o['result'].get('fees') or ([o['result']['fee']] if o['result'].get('fee') else [])
        if dec(o['result']['filled']) and not fees:
            reasons.append('ORDER_FEES_MISSING')
        expected = {}
        for fee in fees:
            expected[fee['currency']] = expected.get(fee['currency'], Decimal(0)) + dec(fee['cost'])
        actual = {ccy: val for (order_id, ccy), val in order_fees.items() if order_id == oid}
        if actual != expected:
            reasons.append('ORDER_FEES_MISMATCH')
    if abs(spot_qty) > Decimal('1e-10') or abs(short_qty) > Decimal('1e-10'):
        reasons.append('FILL_INVENTORY_NOT_FLAT')
    funding = Decimal(0)
    bill_orders = {str(b.get('ordId')) for b in bills if b['type'] == '2'}
    if any(oid not in bill_orders for oid, qty in totals.items() if qty):
        reasons.append('TRADE_BILLS_MISSING')
    for b in bills:
        if b['type'] == '8':
            if b['ccy'] != 'USDT':
                reasons.append('UNPRICED_FUNDING_CURRENCY')
            else:
                funding += dec(b['balChg'])
        elif b['type'] == '2' and str(b.get('ordId')) not in known:
            reasons.append('UNATTRIBUTED_TRADE_BILL')
        elif b['type'] != '2':
            reasons.append('OTHER_BILL_REQUIRES_ATTRIBUTION')
    net = cash - fees_usdt + funding
    return {'status': 'RECONCILED_AS_OF_QUERY' if not reasons else 'RECONCILIATION_REQUIRED',
            'reasons': sorted(set(reasons)), 'funding_usdt': str(funding),
            'fees_by_currency': {k: str(v) for k, v in fee_by_ccy.items()},
            'trade_cashflow_usdt': str(cash), 'net_pnl_usdt': str(net) if not reasons else None,
            'capital_return': None, 'annualized_return': None,
            'funding_coverage': 'API_WINDOW_QUERIED; later archive corrections remain possible',
            'basis': '实际成交现金流与手续费、窗口内资金费率入账；未分配总投入资本',
            'archive_as_of_ms': now_ms}


def collect(ex, p, db_path):
    now = int(time.time() * 1000)
    start = int(p['opened_at'] * 1000)
    end = int(p.get('closed_at', now / 1000) * 1000)
    report = {'status': 'INVALID_DECLARED_GAP', 'net_pnl_usdt': None, 'reasons': []}
    if not p.get('live'):
        return report | {'reasons': ['SIMULATED_POSITION_NO_REAL_ACCOUNTING']}
    if start < now - 89 * DAY_MS:
        return report | {'reasons': ['SOURCE_ARCHIVE_WINDOW_EXCEEDED']}
    # 不自动申请长历史、补造缺口或修改仓位。
    db = sqlite3.connect(db_path)
    try:
        db.execute('CREATE TABLE IF NOT EXISTS evidence (account TEXT, kind TEXT, id TEXT, raw TEXT, PRIMARY KEY(account,kind,id))')
        db.execute('CREATE TABLE IF NOT EXISTS reports (created_ms INTEGER, cycle TEXT, report TEXT)')
        uid = ex.private_get_account_config()['data'][0]['uid']
        account_id = hashlib.sha256(str(uid).encode()).hexdigest()
        fills, bills = [], []
        for symbol, inst_type in [(p['spot'], 'SPOT'), (p['perp'], 'SWAP')]:
            inst_id = ex.markets[symbol]['id']
            params = {'instType': inst_type, 'instId': inst_id, 'begin': str(start), 'end': str(end)}
            batch = pages(ex.private_get_trade_fills_history, params)
            store(db, account_id, 'fills', batch)
            fills.extend(x for x in batch if x['instId'] == inst_id and start <= int(x['ts']) <= end)
        # 拉账户窗口，避免漏掉没有 instId 的利息/账户扣费；其归属不明时阻止净收益结论。
        batch = pages(ex.private_get_account_bills_archive, {'begin': str(start), 'end': str(end)})
        store(db, account_id, 'bills', batch)
        symbols = {ex.markets[p['spot']]['id'], ex.markets[p['perp']]['id']}
        bills = [x for x in batch if start <= int(x['ts']) <= end and
                 (x.get('instId') in symbols or (not x.get('instId') and x['type'] != '1'))]
        report = summarize(p, fills, bills, now)
        # 每次查询证据只增不改；报告说明查询时点，不宣称档案永远不会延迟补发。
    except Exception as exc:
        report['reasons'] = [f'{type(exc).__name__}:{exc}']
    finally:
        db.execute('INSERT INTO reports VALUES (?,?,?)', (now, f'{p["perp"]}:{start}', json.dumps(report)))
        db.commit()
        db.close()
    return report


def main():
    import trade_a
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('coin')
    parser.add_argument('--previous', type=int, default=0, help='0=当前轮，1=上一轮')
    args = parser.parse_args()
    if args.previous < 0:
        parser.error('--previous 必须非负')
    trade_a.LIVE = True  # 只读账户选择；本模块没有下单调用
    p = trade_a.load()[args.coin.upper()]
    for _ in range(args.previous):
        p = p['previous']
    result = collect(trade_a.exchange(), p, trade_a.ROOT / 'accounting_a.sqlite')
    print(json.dumps(result, indent=2, ensure_ascii=False))


if __name__ == '__main__':
    main()
