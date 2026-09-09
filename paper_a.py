"""F08/F09 模拟成交与资金费：公共行情、独立纸面资金；无私有 API。"""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

from accounting import dec
from check_a import MAX_BOOK_AGE_MS, MAX_SLIPPAGE_BPS
from notify_events import save

ROOT = Path(__file__).resolve().parent


def initial(cash, spot_fee, perp_fee):
    cash, spot_fee, perp_fee = map(dec, (cash, spot_fee, perp_fee))
    if cash <= 0 or not 0 <= spot_fee < 1 or not 0 <= perp_fee < 1:
        raise ValueError('INVALID_PAPER_CAPITAL_OR_FEE')
    return {'version': 1, 'mode': 'paper', 'cash_usdt': str(cash), 'initial_cash_usdt': str(cash),
            'fees': {'spot': str(spot_fee), 'perp': str(perp_fee)},
            'fee_basis': 'USER_SPECIFIED_SIMULATION_USDT_NOT_AUTHENTICATED_TIER',
            'positions': {}, 'trades': {}, 'pending': None}


def market(ex, symbol):
    m = ex.markets[symbol]
    if m.get('active') is not True or m.get('quote') != 'USDT':
        raise ValueError('UNSUPPORTED_MARKET')
    if m.get('spot'):
        return 'spot', dec(1)
    if m.get('swap') and m.get('linear') and m.get('settle') == 'USDT':
        size = dec(m['contractSize'])
        if size > 0:
            return 'perp', size
    raise ValueError('UNSUPPORTED_MARKET')


def quantity(ex, symbol, amount):
    amount = dec(amount)
    qty = dec(ex.amount_to_precision(symbol, str(amount)))
    if amount <= 0 or qty <= 0 or qty > amount:
        raise ValueError('INVALID_QUANTITY_PRECISION')
    minimum = ex.markets[symbol].get('limits', {}).get('amount', {}).get('min')
    if minimum is not None and qty < dec(minimum):
        raise ValueError('BELOW_MINIMUM_AMOUNT')
    return qty


def quote(ex, symbol, side, amount, now=None):
    if side not in ('buy', 'sell'):
        raise ValueError('INVALID_SIDE')
    kind, size = market(ex, symbol)
    qty = quantity(ex, symbol, amount)
    book = ex.fetch_order_book(symbol, limit=50)
    now = time.time()*1000 if now is None else now
    ts = dec(book['timestamp'])
    if not 0 <= dec(now)-ts <= MAX_BOOK_AGE_MS:
        raise ValueError('BOOK_STALE_OR_FUTURE')
    # 校验两边顺序、数值；不能用损坏的未成交侧计算中价。
    for name, direction in [('asks', 1), ('bids', -1)]:
        previous = None
        if not book[name]: raise ValueError('BOOK_EMPTY')
        for raw in book[name]:
            px, vol = dec(raw[0]), dec(raw[1])
            if px <= 0 or vol < 0 or (previous is not None and (px-previous)*direction < 0):
                raise ValueError('INVALID_BOOK_LEVEL')
            previous = px
    ask, bid = dec(book['asks'][0][0]), dec(book['bids'][0][0])
    if bid >= ask: raise ValueError('BOOK_CROSSED')
    best = ask if side == 'buy' else bid
    cap = best*(1+dec(MAX_SLIPPAGE_BPS)/10000*(1 if side == 'buy' else -1))
    limit = dec(ex.price_to_precision(symbol, str(cap)))
    if (side == 'buy' and limit > cap) or (side == 'sell' and limit < cap):
        # 取整越过上限时，保留范围内最深档，不能退回最优档丢弃有效深度。
        eligible = [dec(row[0]) for row in book['asks' if side == 'buy' else 'bids']
                    if (dec(row[0]) <= cap if side == 'buy' else dec(row[0]) >= cap)]
        limit = dec(ex.price_to_precision(symbol, str(eligible[-1])))
    if limit <= 0 or (side == 'buy' and limit > cap) or (side == 'sell' and limit < cap):
        raise ValueError('PRICE_PRECISION_OUTSIDE_CAP')
    minimum = ex.markets[symbol].get('limits', {}).get('cost', {}).get('min')
    if minimum is not None and qty*size*limit < dec(minimum):
        raise ValueError('BELOW_MINIMUM_NOTIONAL')
    left, cost, fills = qty, dec(0), []
    for raw in book['asks' if side == 'buy' else 'bids']:
        px, volume = dec(raw[0]), dec(raw[1])
        if (side == 'buy' and px > limit) or (side == 'sell' and px < limit): break
        take = min(left, volume)
        if take:
            fills.append([str(px), str(take)])
            cost += take*px*size
            left -= take
        if not left: break
    # 不可执行的小数尘埃舍去，重新按原层级分配。
    filled = qty-left
    if filled:
        aligned = dec(ex.amount_to_precision(symbol, str(filled)))
        if not 0 <= aligned <= filled: raise ValueError('INVALID_FILL_PRECISION')
        left_fill, cost, aligned_fills = aligned, dec(0), []
        for px, amount in fills:
            take = min(left_fill, dec(amount));left_fill -= take
            if take: aligned_fills.append([px,str(take)]);cost += take*dec(px)*size
        filled, fills = aligned, aligned_fills
    return {'symbol':symbol,'side':side,'kind':kind,'contract_size':str(size),'requested':str(qty),
            'filled':str(filled),'unfilled':str(qty-filled),'notional_usdt':str(cost),
            'vwap':str(cost/filled/size) if filled else None,'limit_price':str(limit),
            'best':str(best),'book_timestamp':int(ts),'levels':fills,
            'slippage_bps':str(abs(cost/filled/size/best-1)*10000) if filled else None,
            'status':'FILLED' if filled==qty else 'PARTIAL_CANCELED' if filled else 'CANCELED'}


def available(state):
    held = sum((dec(p['margin_usdt']) for p in state['positions'].values()), dec(0))
    reserved = dec(state['pending']['reserved_usdt']) if state.get('pending') else dec(0)
    from allocate_a import reserved as allocation_reserved
    return dec(state['cash_usdt'])-held-reserved-allocation_reserved(state)


def apply_fill(state, q):
    """现货现金流；永续空仓 1x 初始占用，平仓释放并计价差，不把卖空名义计为现金。"""
    symbol, kind = q['symbol'], q['kind'];qty, cost = dec(q['filled']), dec(q['notional_usdt'])
    pos = state['positions'].setdefault(symbol, {'kind':kind,'quantity':'0','entry_notional_usdt':'0',
                                               'margin_usdt':'0','contract_size':q['contract_size']})
    if pos['kind'] != kind or pos['contract_size'] != q['contract_size']:
        raise ValueError('MARKET_METADATA_CHANGED')
    old = dec(pos['quantity']);entry = dec(pos['entry_notional_usdt']);margin = dec(pos['margin_usdt'])
    fee = cost*dec(state['fees'][kind]);cash = dec(state['cash_usdt'])-fee
    if kind=='spot':
        if q['side']=='buy': old+=qty;entry+=cost;cash-=cost
        else:
            if qty>old:raise ValueError('SPOT_OVERSELL')
            entry -= entry*qty/old if old else 0;old-=qty;cash+=cost
    elif q['side']=='sell': old+=qty;entry+=cost;margin+=cost
    else:
        if qty>old:raise ValueError('PERP_OVERCLOSE')
        removed=entry*qty/old if old else dec(0)
        cash+=removed-cost;margin-=margin*qty/old if old else 0;entry-=removed;old-=qty
    pos.update(quantity=str(old),entry_notional_usdt=str(entry),margin_usdt=str(margin))
    state['cash_usdt']=str(cash);q['fee_usdt']=str(fee)


def submit(state, path, ex, symbol, side, amount, trade_id):
    if state.get('pending'):raise RuntimeError('PAPER_PENDING_REQUIRES_REVIEW')
    if trade_id not in state['trades']:raise ValueError('UNKNOWN_PAPER_TRADE')
    q=quote(ex,symbol,side,amount)
    kind=q['kind'];qty=dec(q['requested']);pos=state['positions'].get(symbol,{'quantity':'0'})
    if (kind=='spot' and side=='sell') or (kind=='perp' and side=='buy'):
        if qty>dec(pos['quantity']):raise ValueError('PAPER_REDUCE_ONLY_EXCEEDED')
    budget=qty*dec(q['contract_size'])*dec(q['limit_price'] if side=='buy' else q['best'])
    fee=budget*dec(state['fees'][kind])
    if (kind=='spot' and side=='buy') or (kind=='perp' and side=='sell'):
        reserve=budget+fee
    elif kind=='spot':
        reserve=dec(0)  # USDT 手续费从本次现货出售所得扣除。
    else:
        share=qty/dec(pos['quantity'])
        released=(dec(pos['entry_notional_usdt'])+dec(pos['margin_usdt']))*share
        reserve=max(dec(0),budget+fee-released)  # 平空价差和费用，扣除同步释放的保证金。
    opening=(kind=='spot' and side=='buy') or (kind=='perp' and side=='sell')
    buffer=dec(state.get('allocation_limits',{}).get('buffer_usdt','0')) if opening else dec(0)
    if opening and state.get('allocation_limits'):
        ident=state['trades'][trade_id].get('allocation_id')
        allocation=state.get('allocations',{}).get(ident,{})
        if allocation.get('status')!='EXECUTING':raise ValueError('ALLOCATION_REQUIRED')
        spent=sum((dec(o['quote']['notional_usdt']) for o in state['trades'][trade_id]['orders'] if o['quote']['symbol']==symbol and o['quote']['side']==side),dec(0))
        if spent+budget>dec(allocation['leg_caps'][symbol]):raise ValueError('ALLOCATION_LEG_BUDGET_EXCEEDED')
    if available(state)-buffer<reserve:raise ValueError('INSUFFICIENT_PAPER_FUNDS')
    if not 0<=time.time()*1000-q['book_timestamp']<=MAX_BOOK_AGE_MS:raise ValueError('QUOTE_EXPIRED')
    intent={'order_id':uuid.uuid4().hex,'trade_id':trade_id,'reserved_usdt':str(reserve),'quote':q}
    state['pending']=intent;save(path,state)
    # 模拟成交与释放预留一次原子写入；中断留下 pending 时保留，不重新模拟旧盘口。
    new=copy.deepcopy(state);apply_fill(new,q);new['pending']=None
    new['trades'][trade_id]['orders'].append({**intent,'quote':q,'simulated':True,
                                            'executed_at_ms':int(time.time()*1000)})
    save(path,new);state.clear();state.update(new)
    return q


def operate(state, path, ex, coin, notional=None, closing=False, allocation_id=None):
    if state.get('pending'):raise RuntimeError('PAPER_PENDING_REQUIRES_REVIEW')
    if any(a['status']=='EXECUTING' and key!=allocation_id for key,a in state.get('allocations',{}).items()):
        raise RuntimeError('ALLOCATION_EXECUTION_REQUIRES_REVIEW')
    spot,perp=f'{coin}/USDT',f'{coin}/USDT:USDT'
    active=[(k,t) for k,t in state['trades'].items() if t['phase']!='closed']
    if closing:
        matches=[(k,t) for k,t in active if t['coin']==coin]
        if len(matches)!=1:raise ValueError('PAPER_TRADE_NOT_UNIQUE')
        ident,trade=matches[0];trade['phase']='closing';save(path,state)
        legs=[(perp,'buy'),(spot,'sell')]
    else:
        if state.get('allocation_limits'):
            allocation=state.get('allocations',{}).get(allocation_id,{})
            if allocation.get('status')!='EXECUTING' or allocation.get('coin')!=coin:raise ValueError('ALLOCATION_REQUIRED')
            if any(t['coin']==coin or t['phase']!='open' for _,t in active):raise RuntimeError('PAPER_EXISTING_TRADE_REQUIRES_CLOSE')
        elif active:raise RuntimeError('PAPER_EXISTING_TRADE_REQUIRES_CLOSE')
        _,size=market(ex,perp)
        if allocation_id is not None:
            ranked=allocation['ranking']
            base=quantity(ex,spot,ranked['base_quantity'])
            contracts=quantity(ex,perp,ranked['contracts'])
            if base!=dec(ranked['base_quantity']) or contracts!=dec(ranked['contracts']) or base!=contracts*size:
                raise ValueError('RANKED_QUANTITY_OR_MARKET_CHANGED')
        else:
            qspot=quote(ex,spot,'buy',dec(notional)/dec(ex.fetch_ticker(spot)['last']))
            contracts=quantity(ex,perp,dec(qspot['requested'])/size)
            base=quantity(ex,spot,contracts*size)
        if abs(base-contracts*size)>max(base,contracts*size)*dec('.02'):raise ValueError('PLAN_EXPOSURE')
        qspot=quote(ex,spot,'buy',base);qperp=quote(ex,perp,'sell',contracts)
        need=base*dec(qspot['limit_price'])*(1+dec(state['fees']['spot']))+contracts*size*dec(qperp['best'])*(1+dec(state['fees']['perp']))
        if available(state)<need:raise ValueError('INSUFFICIENT_PAIR_CAPITAL')
        ident=uuid.uuid4().hex
        state['trades'][ident]={'trade_id':ident,'coin':coin,'phase':'opening','targets':{'base':str(base),'contracts':str(contracts)},'orders':[],'opened_at':time.time(),'allocation_id':allocation_id}
        save(path,state);legs=[(spot,'buy'),(perp,'sell')]
    try:
        for symbol,side in legs:
            if closing:qty=dec(state['positions'].get(symbol,{}).get('quantity','0'))
            elif symbol==spot:qty=base
            else:qty=quantity(ex,perp,dec(state['positions'][spot]['quantity'])/size)
            if qty==0:continue
            q=submit(state,path,ex,symbol,side,qty,ident)
            if closing and dec(q['unfilled'])>0:break  # 空单未全平，不拆现货腿。
            if not closing and symbol==spot and dec(q['filled'])==0:break
        trade=state['trades'][ident]
        b=dec(state['positions'].get(spot,{}).get('quantity','0'));c=dec(state['positions'].get(perp,{}).get('quantity','0'))
        _,unit=market(ex,perp);trade['exposure_base']=str(b-c*unit)
        trade['phase']='closed' if b==c==0 else ('open' if not closing and c>0 and abs(b-c*unit)<=max(b,c*unit)*dec('.02') else 'needs_close')
        if trade['phase']=='closed':trade['closed_at']=time.time()
        save(path,state)
    except Exception as exc:
        # 若成交的原子写失败，不再用内存覆盖磁盘中的 pending。
        persisted=json.loads(path.read_text())
        persisted['trades'][ident]['phase']='needs_close'
        persisted['trades'][ident]['gap']='PAPER_LEG_FAILED:'+type(exc).__name__
        b=dec(persisted['positions'].get(spot,{}).get('quantity','0'))
        perp_pos=persisted['positions'].get(perp,{})
        persisted['trades'][ident]['exposure_base']=str(b-dec(perp_pos.get('quantity','0'))*dec(perp_pos.get('contract_size','0')))
        save(path,persisted);state.clear();state.update(persisted)
        raise
    return state['trades'][ident]


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['init','status','open','close','settle'])
    parser.add_argument('coin',nargs='?');parser.add_argument('--notional')
    parser.add_argument('--at-ms',type=int,help='settle: exact public funding timestamp in milliseconds')
    parser.add_argument('--cash');parser.add_argument('--spot-fee');parser.add_argument('--perp-fee')
    args=parser.parse_args();path=ROOT/'paper_a_state.json'
    with path.with_suffix('.lock').open('a') as lock:
        os.fchmod(lock.fileno(),0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        if args.command=='init':
            if path.exists():raise RuntimeError('PAPER_STATE_EXISTS_NO_RESET')
            if not all((args.cash,args.spot_fee,args.perp_fee)):parser.error('init requires cash/spot-fee/perp-fee')
            state=initial(args.cash,args.spot_fee,args.perp_fee);save(path,state)
        else:
            state=json.loads(path.read_text())
            if state.get('version')!=1 or state.get('mode')!='paper':raise ValueError('INVALID_PAPER_STATE')
            if args.command in ('open','close','settle'):
                if not args.coin or (args.command=='open' and not args.notional):parser.error('coin/notional required')
                import ccxt
                from scan import PROXY
                ex=ccxt.okx({'timeout':30000,**({'httpsProxy':PROXY} if PROXY else {})})
                if args.command=='settle':
                    if args.at_ms is None:parser.error('settle requires --at-ms')
                    from paper_funding import settle
                    settle(state,path,ex,f'{args.coin.upper()}/USDT:USDT',args.at_ms)
                else:
                    ex.load_markets()
                    operate(state,path,ex,args.coin.upper(),args.notional,args.command=='close')
        print(json.dumps(state,ensure_ascii=False,indent=2))


if __name__=='__main__':main()
