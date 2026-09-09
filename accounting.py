"""OKX 只读账目采集。SQLite 保存原始证据；资料不足时净损益为 null。"""
import argparse
from datetime import datetime, timezone
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


def summarize(p, fills, bills, now_ms, instrument_ids=None):
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
        if instrument_ids is not None and f.get('instId') != instrument_ids[o['leg']]:
            raise ValueError('FILL_INSTRUMENT_MISMATCH')
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


# 查询重叠七天，用于发现迟发记录；更早更正仅在显式重查窗口时可见。
OVERLAP_MS = 7 * DAY_MS


def init_db(db):
    db.executescript('''
        CREATE TABLE IF NOT EXISTS evidence (account TEXT, kind TEXT, id TEXT, raw TEXT,
            PRIMARY KEY(account,kind,id));
        CREATE TABLE IF NOT EXISTS reports (created_ms INTEGER, cycle TEXT, report TEXT);
        CREATE TABLE IF NOT EXISTS collection_windows (
            account TEXT, kind TEXT, scope TEXT, begin_ms INTEGER, end_ms INTEGER,
            cursor TEXT, completed INTEGER NOT NULL DEFAULT 0, checked_ms INTEGER,
            PRIMARY KEY(account,kind,scope,begin_ms,end_ms));
        CREATE TABLE IF NOT EXISTS evidence_conflicts (
            account TEXT, kind TEXT, id TEXT, original TEXT, observed TEXT, checked_ms INTEGER,
            PRIMARY KEY(account,kind,id,observed));
    ''')


def covered(db, account, kind, scope, start, end, broad_scope=None):
    edge = start
    for left, right in db.execute('''SELECT begin_ms,end_ms FROM collection_windows
            WHERE account=? AND kind=? AND (scope=? OR scope=?) AND completed=1 ORDER BY begin_ms''',
            (account, kind, scope, broad_scope)):
        if left > edge:
            break
        edge = max(edge, right)
        if edge >= end:
            return True
    return False


def sync_stream(db, account, kind, scope, method, params, start, end, now):
    floor = now - 89 * DAY_MS
    pending = db.execute('''SELECT begin_ms,end_ms,cursor FROM collection_windows
        WHERE account=? AND kind=? AND scope=? AND completed=0 ORDER BY begin_ms LIMIT 1''',
        (account, kind, scope)).fetchone()
    # 过期的未完成窗口保留历史事实；不能把它标成已完整采集。
    if pending and pending[0] < floor:
        pending = None
    watermark = db.execute('''SELECT MAX(end_ms) FROM collection_windows
        WHERE account=? AND kind=? AND scope=? AND completed=1''', (account,kind,scope)).fetchone()[0]
    begin = max(start, floor)
    if watermark is not None and covered(db, account,kind,scope,start,min(end,watermark)):
        begin = max(begin, min(end,watermark)-OVERLAP_MS)
    if pending:
        begin, stop, cursor = pending
    else:
        stop, cursor = end, None
        if begin > stop:
            return
        # 完成窗口可重查，不能清除旧覆盖证明；进度以新 end 区分。
        prior = db.execute('''SELECT completed FROM collection_windows WHERE
            account=? AND kind=? AND scope=? AND begin_ms=? AND end_ms=?''',
            (account,kind,scope,begin,stop)).fetchone()
        if not prior:
            db.execute('INSERT INTO collection_windows VALUES (?,?,?,?,?,NULL,0,NULL)',
                       (account,kind,scope,begin,stop));db.commit()
    for _ in range(1000):
        response = method({**params,'begin':str(begin),'end':str(stop),'limit':'100',
                           **({'after':cursor} if cursor else {})})
        if str(response.get('code')) != '0' or not isinstance(response.get('data'),list):
            raise ValueError('INVALID_API_RESPONSE')
        batch = response['data']
        if not batch:
            db.execute('''UPDATE collection_windows SET completed=1,cursor=NULL,checked_ms=?
                WHERE account=? AND kind=? AND scope=? AND begin_ms=? AND end_ms=?''',
                (now,account,kind,scope,begin,stop));db.commit()
            # 已恢复旧窗口后，继续查询到当前 end，避免中断期间形成未查询尾部。
            if stop < end:
                sync_stream(db,account,kind,scope,method,params,start,end,now)
            return
        ids = [int(row['billId']) for row in batch]
        if min(ids) < 0 or (cursor and max(ids) >= int(cursor)):
            raise ValueError('PAGINATION_NOT_ADVANCING')
        for row in batch:
            ts = int(row['ts'])
            if not begin <= ts <= stop:
                raise ValueError('RESPONSE_OUTSIDE_QUERY_WINDOW')
            if 'instId' in params and row.get('instId') != params['instId']:
                raise ValueError('RESPONSE_INSTRUMENT_MISMATCH')
            raw=json.dumps(row,sort_keys=True,separators=(',',':'))
            old=db.execute('SELECT raw FROM evidence WHERE account=? AND kind=? AND id=?',
                           (account,kind,str(row['billId']))).fetchone()
            if old and old[0]!=raw:
                db.execute('INSERT OR IGNORE INTO evidence_conflicts VALUES (?,?,?,?,?,?)',
                           (account,kind,str(row['billId']),old[0],raw,now));db.commit()
                raise ValueError('IMMUTABLE_EVIDENCE_CONFLICT')
            store(db,account,kind,[row])
        cursor=str(min(ids))
        db.execute('''UPDATE collection_windows SET cursor=? WHERE account=? AND kind=? AND scope=?
            AND begin_ms=? AND end_ms=? AND completed=0''',(cursor,account,kind,scope,begin,stop))
        db.commit()  # 原始记录与游标同一事务；中断后从确认页继续。
    raise ValueError('PAGINATION_LIMIT')


def sync_evidence(ex, db, start, end, now, instruments):
    uid = ex.private_get_account_config()['data'][0]['uid']
    if not isinstance(uid,str) or not uid:
        raise ValueError('ACCOUNT_ID_UNKNOWN')
    account=hashlib.sha256(uid.encode()).hexdigest()
    streams=[('fills',inst if inst != '*' else 'ALL:'+typ,ex.private_get_trade_fills_history,
              {'instType':typ, **({'instId':inst} if inst != '*' else {})})
             for inst,typ in instruments]
    streams.append(('bills','ACCOUNT',ex.private_get_account_bills_archive,{}))
    errors=[]
    for kind,scope,method,params in streams:
        try:
            sync_stream(db,account,kind,scope,method,params,start,end,now)
        except Exception as exc:
            db.rollback()
            errors.append(f'{kind}:{type(exc).__name__}:{exc}')
    gaps=[f'SOURCE_ARCHIVE_WINDOW_EXCEEDED:{kind}:{scope}' for kind,scope,_,params in streams
          if not covered(db,account,kind,scope,start,end,
              'ALL:'+params['instType'] if kind=='fills' else None)]
    if db.execute('SELECT 1 FROM evidence_conflicts WHERE account=? LIMIT 1',(account,)).fetchone():
        gaps.append('EVIDENCE_CONFLICT_REQUIRES_REVIEW')
    return account,errors+gaps


def collect(ex, p, db_path):
    now=int(time.time()*1000)
    start=int(p['opened_at']*1000)
    end=int(p.get('closed_at',now/1000)*1000)
    report={'status':'INVALID_DECLARED_GAP','net_pnl_usdt':None,'reasons':[]}
    if not p.get('live'):
        return report | {'reasons':['SIMULATED_POSITION_NO_REAL_ACCOUNTING']}
    if start > end or end > now:
        return report | {'reasons':['INVALID_COLLECTION_WINDOW']}
    # 没有本地证据且整笔交易已在窗口之外时，不申请长历史或补造数据。
    if start < now-89*DAY_MS and (str(db_path)==':memory:' or not Path(db_path).exists()):
        return report | {'reasons':['SOURCE_ARCHIVE_WINDOW_EXCEEDED']}
    db=sqlite3.connect(db_path)
    try:
        init_db(db)
        instruments=[(ex.markets[p['spot']]['id'],'SPOT'),(ex.markets[p['perp']]['id'],'SWAP')]
        account,gaps=sync_evidence(ex,db,start,end,now,instruments)
        if gaps:
            report['reasons']=gaps
        else:
            symbols={inst for inst,_ in instruments}
            fills=[];bills=[]
            for kind,raw in db.execute('SELECT kind,raw FROM evidence WHERE account=?',(account,)):
                row=json.loads(raw)
                if not start<=int(row['ts'])<=end:
                    continue
                if kind=='fills' and row.get('instId') in symbols:
                    fills.append(row)
                if kind=='bills' and (row.get('instId') in symbols or
                        (not row.get('instId') and row['type']!='1')):
                    bills.append(row)
            report=summarize(p,fills,bills,now,{'base':instruments[0][0],'contracts':instruments[1][0]})
            report['funding_coverage']='PERSISTED_QUERY_WINDOWS; 7-day overlap; later corrections remain possible'
    except Exception as exc:
        db.rollback()
        report['reasons']=[f'{type(exc).__name__}:{exc}']
    finally:
        db.execute('INSERT INTO reports VALUES (?,?,?)',(now,f'{p["perp"]}:{start}',json.dumps(report)))
        db.commit();db.close()
    return report


def collect_account(ex, db_path, start):
    now=int(time.time()*1000)
    if start < 0 or start > now:
        raise ValueError('INVALID_COLLECTION_WINDOW')
    db=sqlite3.connect(db_path)
    try:
        init_db(db)
        account,gaps=sync_evidence(ex,db,start,now,now,[('*','SPOT'),('*','SWAP')])
        counts=dict(db.execute('SELECT kind,COUNT(*) FROM evidence WHERE account=? GROUP BY kind',(account,)))
        return {'status':'INVALID_DECLARED_GAP' if gaps else 'EVIDENCE_COLLECTED',
                'reasons':gaps,'evidence_counts':counts,'net_pnl_usdt':None,
                'basis':'账户级原始证据，未归属本策略；不推断策略持仓或收益',
                'requested_since_ms':start,'queried_at_ms':now}
    finally:
        db.close()


def main():
    import trade_a
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('coin', nargs='?')
    parser.add_argument('--previous', type=int, default=0, help='0=当前轮，1=上一轮')
    parser.add_argument('--account-sync',action='store_true',help='仅增量采集账户证据，不归属现有持仓')
    parser.add_argument('--since',help='账户采集起点，UTC 日期 YYYY-MM-DD')
    parser.add_argument('--interval',type=int,default=0,help='0=单次；持续采集至少 60 秒一次')
    args = parser.parse_args()
    if args.previous < 0 or (args.interval != 0 and args.interval < 60):
        parser.error('previous 非负，interval 为 0 或至少 60')
    if args.account_sync:
        if args.coin or args.previous or not args.since:
            parser.error('--account-sync 需要 --since，不能指定 coin/previous')
        try:
            start=int(datetime.strptime(args.since,'%Y-%m-%d').replace(tzinfo=timezone.utc).timestamp()*1000)
        except ValueError:
            parser.error('--since 必须是 UTC 日期 YYYY-MM-DD')
    elif not args.coin or args.since:
        parser.error('指定 coin，或使用 --account-sync --since')
    trade_a.LIVE = True  # 只读账户选择；本模块没有下单调用
    ex=trade_a.exchange()
    while True:
        if args.account_sync:
            result=collect_account(ex,trade_a.ROOT/'accounting_a.sqlite',start)
        else:
            p=trade_a.load()[args.coin.upper()]
            for _ in range(args.previous):p=p['previous']
            result=collect(ex,p,trade_a.ROOT/'accounting_a.sqlite')
        print(json.dumps(result,ensure_ascii=False),flush=True)
        if not args.interval:
            break
        time.sleep(args.interval)


if __name__ == '__main__':
    main()
