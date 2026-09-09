"""F09 单个真实结算周期的纸面资金费；不接触真实账户账单。"""
import copy
import time

from accounting import dec
from notify_events import save


def integer(value):
    n = dec(value)
    if n != int(n) or n < 0:
        raise ValueError('INVALID_TIMESTAMP')
    return int(n)


def held_base(state, symbol, at, now):
    """从已确认模拟成交回放；同毫秒跨界或旧订单没有成交时间时不猜测。"""
    if state.get('pending'):
        raise ValueError('PENDING_EXECUTION')
    events, seen = [], set()
    for trade_id, trade in state['trades'].items():
        for order in trade['orders']:
            q = order['quote']
            if q['symbol'] != symbol:
                continue
            if not order.get('simulated') or order['trade_id'] != trade_id:
                raise ValueError('INVALID_ORDER_IDENTITY')
            if order['order_id'] in seen:
                raise ValueError('DUPLICATE_ORDER_ID')
            seen.add(order['order_id'])
            qty, size = dec(q['filled']), dec(q['contract_size'])
            if qty < 0 or size <= 0 or q['kind'] != 'perp' or q['side'] not in ('buy', 'sell'):
                raise ValueError('INVALID_FILL')
            if not qty:
                continue
            if order.get('executed_at_ms') is None:
                raise ValueError('LEGACY_FILL_TIME_MISSING')
            ts = integer(order['executed_at_ms'])
            if ts > now:
                raise ValueError('FILL_TIME_FUTURE')
            if ts == at:
                raise ValueError('SETTLEMENT_BOUNDARY_AMBIGUOUS')
            events.append((ts, qty*size*(1 if q['side']=='sell' else -1)))
    total, held = dec(0), dec(0)
    for ts, change in sorted(events, key=lambda e: e[0]):
        total += change
        if total < 0:
            raise ValueError('INVALID_POSITION_HISTORY')
        if ts < at:
            held += change
    pos = state['positions'].get(symbol)
    current = dec(pos['quantity'])*dec(pos['contract_size']) if pos else dec(0)
    if current != total:
        raise ValueError('POSITION_HISTORY_MISMATCH')
    return held


def public_cycle(ex, symbol, at):
    if not ex.markets:
        ex.load_markets()
    market = ex.markets[symbol]
    if not (market.get('swap') and market.get('linear') and market.get('settle')=='USDT'):
        raise ValueError('UNSUPPORTED_MARKET')
    inst = market['id']
    response = ex.publicGetPublicFundingRateHistory(
        {'instId':inst, 'before':str(at-1), 'after':str(at+1), 'limit':'100'})
    if str(response.get('code')) != '0':
        raise ValueError('FUNDING_API_ERROR')
    rows = [r for r in response['data'] if integer(r['fundingTime'])==at and r['instId']==inst]
    if len(rows)!=1:
        raise ValueError('SETTLED_RATE_MISSING_OR_DUPLICATE')
    row = rows[0]
    if row.get('realizedRate') in (None, ''):
        raise ValueError('REALIZED_RATE_MISSING')
    return row, dec(row['realizedRate'])  # 不使用预测 fundingRate。


def public_price(ex, symbol, at):
    response = ex.publicGetMarketHistoryMarkPriceCandles(
        {'instId':ex.markets[symbol]['id'], 'bar':'1m',
         'before':str(at-1), 'after':str(at+60000), 'limit':'100'})
    if str(response.get('code'))!='0':
        raise ValueError('MARK_API_ERROR')
    rows = [r for r in response['data'] if integer(r[0])==at]
    if len(rows)!=1 or len(rows[0])!=6 or str(rows[0][5])!='1':
        raise ValueError('CONFIRMED_MARK_CANDLE_MISSING')
    row=rows[0];op,hi,lo,cl=map(dec,row[1:5])
    if not 0<lo<=min(op,cl)<=max(op,cl)<=hi:
        raise ValueError('INVALID_MARK_CANDLE')
    return row,op


def settle(state, path, ex, symbol, at, now=None):
    if state.get('version')!=1 or state.get('mode')!='paper':
        raise ValueError('INVALID_PAPER_STATE')
    now=integer(int(time.time()*1000) if now is None else now);at=integer(at)
    if at<=0 or at>now-60000:
        raise ValueError('CYCLE_NOT_READY')
    key=f'{symbol}:{at}'
    prior=state.get('funding',{}).get(key)
    if prior and prior['status'] in ('POSTED','NO_POSITION'):
        return prior  # 包括零费率，不因重启或重复调用再记现金。
    entry={'symbol':symbol,'funding_time_ms':at,'checked_at_ms':now,
           'mode':'paper','scope':'SINGLE_REQUESTED_CYCLE_NOT_FULL_COVERAGE',
           'valuation_basis':'SIMULATED_MARK_1M_OPEN_NOT_EXACT_EXCHANGE_BILL',
           'status':'INVALID_DECLARED_GAP','payment_usdt':None,'gap':None}
    try:
        base=held_base(state,symbol,at,now)
        row,rate=public_cycle(ex,symbol,at)
        entry.update(base_short=str(base),realized_rate=str(rate),rate_evidence=row)
        if base==0:
            entry.update(status='NO_POSITION',payment_usdt='0')
        else:
            candle,price=public_price(ex,symbol,at)
            entry.update(status='POSTED',payment_usdt=str(base*price*rate),
                         mark_price=str(price),price_evidence=candle)
    except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
        entry['gap']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
    except Exception as exc:
        # 公共网络/交易所失败记录类型；不输出可能含连接配置的原异常。
        entry['gap']='PUBLIC_DATA_ERROR:'+type(exc).__name__
    new=copy.deepcopy(state)
    new.setdefault('funding',{})[key]=entry
    if entry['status']=='POSTED':
        new['cash_usdt']=str(dec(new['cash_usdt'])+dec(entry['payment_usdt']))
    # 现金与结算去重记录一起原子落盘；失败不能先改内存资金。
    save(path,new);state.clear();state.update(new)
    return entry
