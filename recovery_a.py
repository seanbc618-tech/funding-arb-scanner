"""F15 paper 恢复：核对原子账本，放弃未入账模拟意图，只退出已知余量。"""
import copy
import time
from accounting import dec
from notify_events import save
import paper_a
import paper_funding


def audit(state):
    if state.get('mode') != 'paper' or state.get('version') != 1:
        raise ValueError('INVALID_PAPER_STATE')
    replay = paper_a.initial(state['initial_cash_usdt'], state['fees']['spot'], state['fees']['perp'])
    events, seen, active = [], set(), set()
    for ident, trade in state['trades'].items():
        if trade['trade_id'] != ident or trade['phase'] not in ('opening','open','closing','needs_close','closed'):
            raise ValueError('INVALID_TRADE_IDENTITY_OR_PHASE')
        if trade['phase'] != 'closed':
            if trade['coin'] in active: raise ValueError('DUPLICATE_ACTIVE_COIN')
            active.add(trade['coin'])
        totals = {'spot': dec(0), 'perp': dec(0)}
        previous = 0
        for order in trade['orders']:
            q = order['quote']; kind = q['kind']; oid = order['order_id']
            symbol = f"{trade['coin']}/USDT" + (':USDT' if kind == 'perp' else '')
            if not oid or oid in seen or order.get('simulated') is not True or order['trade_id'] != ident:
                raise ValueError('INVALID_ORDER_IDENTITY')
            seen.add(oid)
            if kind not in totals or q['symbol'] != symbol or q['side'] not in ('buy','sell'):
                raise ValueError('INVALID_FILL_SYMBOL_OR_SIDE')
            ts = paper_funding.integer(order['executed_at_ms'])
            if ts < previous or ts > time.time()*1000 or ts < dec(trade['opened_at'])*1000-1:
                raise ValueError('INVALID_FILL_TIME')
            previous = ts
            qty, requested, size, cost = map(dec, (q['filled'],q['requested'],q['contract_size'],q['notional_usdt']))
            if requested <= 0 or not 0 <= qty <= requested or size <= 0 or (kind == 'spot' and size != 1):
                raise ValueError('INVALID_FILL_QUANTITY')
            status = 'FILLED' if qty == requested else 'PARTIAL_CANCELED' if qty else 'CANCELED'
            if q['status'] != status or dec(q['unfilled']) != requested-qty:
                raise ValueError('NONTERMINAL_OR_INCONSISTENT_FILL')
            if any(dec(px)<=0 or dec(n)<=0 for px,n in q['levels']):raise ValueError('INVALID_FILL_LEVELS')
            if sum((dec(n) for _,n in q['levels']),dec(0)) != qty or sum((dec(px)*dec(n)*size for px,n in q['levels']),dec(0)) != cost:
                raise ValueError('FILL_COST_MISMATCH')
            if dec(q['fee_usdt']) != cost*dec(state['fees'][kind]):raise ValueError('FILL_FEE_MISMATCH')
            totals[kind] += qty*(1 if (kind=='spot' and q['side']=='buy') or (kind=='perp' and q['side']=='sell') else -1)
            if totals[kind] < 0:raise ValueError('TRADE_OVERCLOSE')
            events.append((ts, len(events), copy.deepcopy(q)))
        if trade['phase']=='closed' and any(totals.values()):raise ValueError('CLOSED_WITH_REMAINDER')
    for _,_,q in sorted(events):paper_a.apply_fill(replay,q)
    for symbol in set(replay['positions']) | set(state['positions']):
        actual=state['positions'].get(symbol); expected=replay['positions'].get(symbol)
        if actual is None or expected is None:raise ValueError('POSITION_HISTORY_MISMATCH')
        for key in ('kind','contract_size'):
            if actual[key]!=expected[key]:raise ValueError('POSITION_METADATA_MISMATCH')
        for key in ('quantity','entry_notional_usdt','margin_usdt'):
            if abs(dec(actual[key])-dec(expected[key]))>dec('1e-18'):raise ValueError('POSITION_HISTORY_MISMATCH')
    paid=dec(0)
    for key,entry in state.get('funding',{}).items():
        if key != f"{entry['symbol']}:{entry['funding_time_ms']}":raise ValueError('INVALID_FUNDING_IDENTITY')
        if entry['status']=='POSTED':
            if dec(entry['payment_usdt'])!=dec(entry['base_short'])*dec(entry['mark_price'])*dec(entry['realized_rate']):
                raise ValueError('FUNDING_PAYMENT_MISMATCH')
            known=copy.deepcopy(state);known['pending']=None
            if dec(entry['base_short'])!=paper_funding.held_base(known,entry['symbol'],entry['funding_time_ms'],int(time.time()*1000)):
                raise ValueError('FUNDING_HOLDING_MISMATCH')
            if entry['rate_evidence'].get('realizedRate') is None or dec(entry['rate_evidence']['realizedRate'])!=dec(entry['realized_rate']) or int(entry['rate_evidence']['fundingTime'])!=entry['funding_time_ms']:
                raise ValueError('FUNDING_RATE_EVIDENCE_MISMATCH')
            if int(entry['price_evidence'][0])!=entry['funding_time_ms'] or dec(entry['price_evidence'][1])!=dec(entry['mark_price']) or str(entry['price_evidence'][5])!='1':
                raise ValueError('FUNDING_PRICE_EVIDENCE_MISMATCH')
            paid+=dec(entry['payment_usdt'])
        elif entry['status']=='NO_POSITION':
            known=copy.deepcopy(state);known['pending']=None
            if dec(entry['payment_usdt'])!=0 or paper_funding.held_base(known,entry['symbol'],entry['funding_time_ms'],int(time.time()*1000))!=0:
                raise ValueError('FUNDING_NO_POSITION_MISMATCH')
        elif entry['status']!='INVALID_DECLARED_GAP':raise ValueError('UNKNOWN_FUNDING_STATUS')
    if abs(dec(state['cash_usdt'])-dec(replay['cash_usdt'])-paid)>dec('1e-8'):
        raise ValueError('CASH_HISTORY_MISMATCH')
    return {'orders':len(seen),'funding_usdt':str(paid),'cash_usdt':state['cash_usdt']}


def run(state,path):
    """须持 paper 账本锁；任何不一致先返回，不修改账本、不查询或交易真实账户。"""
    try:
        evidence=audit(state)
        pending=state.get('pending')
        if pending:
            if pending['trade_id'] not in state['trades'] or not pending['order_id']:
                raise ValueError('UNKNOWN_PENDING_IDENTITY')
            if any(o['order_id']==pending['order_id'] for t in state['trades'].values() for o in t['orders']):
                raise ValueError('PENDING_COMMITTED_ATOMICITY_CONFLICT')
            trade=state['trades'][pending['trade_id']];q=pending['quote']
            if trade['phase']=='closed' or q['symbol'] not in (f"{trade['coin']}/USDT",f"{trade['coin']}/USDT:USDT"):
                raise ValueError('UNKNOWN_PENDING_OWNER')
            if dec(pending['reserved_usdt'])<0:raise ValueError('INVALID_PENDING_RESERVE')
    except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
        return {'status':'BLOCK','gap':str(exc) if isinstance(exc,ValueError) else type(exc).__name__}
    new=copy.deepcopy(state);actions=[]
    if pending:
        # apply_fill + 清 pending + 成交记录是同一次原子替换。磁盘无记录且全账回放吻合，
        # 证明此 paper 意图未入账。只放弃它，绝不把旧盘口当现在成交或查询真实订单。
        new['pending']=None
        actions.append({'action':'ABANDONED_UNAPPLIED_PAPER_INTENT','order_id':pending['order_id'],
                        'trade_id':pending['trade_id'],'intent':copy.deepcopy(pending)})
        new['trades'][pending['trade_id']]['phase']='needs_close'
    for ident,a in new.get('allocations',{}).items():
        if a['status']=='EXECUTING':
            matches=[t for t in new['trades'].values() if t.get('allocation_id')==ident]
            if len(matches)>1:return {'status':'BLOCK','gap':'DUPLICATE_ALLOCATION_TRADE'}
            a['status']='FINISHED' if matches and matches[0]['phase']=='open' else 'FAILED'
            if matches:a['trade_id']=matches[0]['trade_id']
            actions.append({'action':'RECONCILED_ALLOCATION','allocation_id':ident,'status':a['status']})
        elif a['status']=='RESERVED' and a.get('owner')=='paper_loop':
            a['status']='RELEASED';actions.append({'action':'RELEASED_UNEXECUTED_RESERVATION','allocation_id':ident})
    for ident,t in new['trades'].items():
        if t['phase'] not in ('opening','closing','needs_close'):continue
        symbols=(f"{t['coin']}/USDT",f"{t['coin']}/USDT:USDT")
        if all(dec(new['positions'].get(s,{}).get('quantity','0'))==0 for s in symbols):
            t.update(phase='closed',closed_at=time.time(),outcome='RECOVERED_CONFIRMED_FLAT')
            actions.append({'action':'CONFIRMED_FLAT','trade_id':ident})
        elif not t.get('exit_request'):
            t['phase']='needs_close';t['exit_request']={'reason':'RECOVERY_KNOWN_REMAINDER','priority':1,'requested_at':time.time()}
            actions.append({'action':'EXIT_KNOWN_REMAINDER','trade_id':ident})
    if actions:
        new.setdefault('recoveries',[]).append({'at_ms':int(time.time()*1000),'actions':actions,'audit':evidence})
        save(path,new);state.clear();state.update(new)
    return {'status':'RECOVERED' if actions else 'CLEAN','actions':actions,'audit':evidence}
