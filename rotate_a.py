"""F14 paper 换仓；同窗口、原仓可释放预算，先平后重新报价，失败可持币为空。"""
import copy
import time
import uuid
from accounting import dec
from notify_events import save
import allocate_a
import paper_a


def policy(values):
    result={k:str(dec(values[k])) for k in ('min_hold_hours','cooldown_hours','buffer_usdt','confirm_seconds')}
    for k in ('confirmations','max_daily'):
        n=dec(values[k])
        if n<1 or n!=int(n):raise ValueError('INVALID_SWITCH_POLICY')
        result[k]=int(n)
    if dec(result['min_hold_hours'])<0 or dec(result['buffer_usdt'])<0 or dec(result['cooldown_hours'])<=0 or dec(result['confirm_seconds'])<=0:
        raise ValueError('INVALID_SWITCH_POLICY')
    return result


def fresh(row):
    now=dec(time.time()*1000)
    if row['status'] not in ('CANDIDATE','REJECT_NET') or not 0<=now-dec(row['computed_at_ms'])<=5000 or dec(row['next_funding_ms'])<=now:
        raise ValueError('STALE_OR_INVALID_SWITCH_FORECAST')
    if len(row['quotes'])!=4 or any(q['status']!='FILLED' or not 0<=now-dec(q['book_timestamp'])<=5000 for q in row['quotes']):
        raise ValueError('STALE_OR_PARTIAL_SWITCH_BOOK')


def required_capital(row):
    return sum((dec(q['requested'])*dec(q['contract_size'])*dec(q['limit_price'] if q['side']=='buy' else q['best'])*(1+dec(row['fees'][q['symbol']])) for q in row['quotes'][:2]),dec(0))


def released_capital(state,trade,row):
    fresh(row)
    pe=state['positions'][f"{trade['coin']}/USDT:USDT"]
    spout,peout=row['quotes'][2:]
    fees=sum((dec(q['notional_usdt'])*dec(state['fees'][q['kind']]) for q in (spout,peout)),dec(0))
    return dec(spout['notional_usdt'])+dec(pe['entry_notional_usdt'])-dec(peout['notional_usdt'])+dec(pe['margin_usdt'])-fees


def fit_ranking(ex,ranking,scenario,budget):
    import rank_a
    rows=[]
    for source in ranking['rows']:
        row=source
        if row['status']=='CANDIDATE' and required_capital(row)>budget and budget>0:
            # 精确比例缩小名义后重新取盘口并向下取精度；后续预算检查仍以新报价为准。
            config=dict(scenario);config['notional']=str(dec(config['notional'])*budget/required_capital(row))
            row=rank_a.evaluate(ex,row['coin'],**config)
        rows.append(copy.deepcopy(row))
    eligible=sorted((r for r in rows if r['status']=='CANDIDATE'),key=lambda r:(-dec(r['capital_return']),r['coin']))
    for i,r in enumerate(eligible,1):r['rank']=i
    return {'rows':rows}


def economics(state,trade,old,new,buffer):
    fresh(old);fresh(new)
    if old['coin']!=trade['coin'] or old['coin']==new['coin'] or dec(old['hold_hours'])!=dec(new['hold_hours']):
        raise ValueError('SWITCH_WINDOW_OR_COIN_MISMATCH')
    spot,perp=f"{trade['coin']}/USDT",f"{trade['coin']}/USDT:USDT"
    sp,pe=state['positions'][spot],state['positions'][perp]
    if dec(old['base_quantity'])!=dec(sp['quantity']) or dec(old['contracts'])!=dec(pe['quantity']) or dec(sp['quantity'])!=dec(pe['quantity'])*dec(pe['contract_size']):
        raise ValueError('SWITCH_HOLDING_QUANTITY_MISMATCH')
    for row in (old,new):
        for q in row['quotes']:
            if dec(row['fees'][q['symbol']])!=dec(state['fees'][q['kind']]):raise ValueError('SWITCH_FEE_MISMATCH')
    spout,peout=old['quotes'][2:]
    exit_fees=sum((dec(q['notional_usdt'])*dec(state['fees'][q['kind']]) for q in (spout,peout)),dec(0))
    # 可释放资本含现货变现、空仓已实现价差、释放保证金；不挪用其他仓/备用金扩仓。
    released=dec(spout['notional_usdt'])+dec(pe['entry_notional_usdt'])-dec(peout['notional_usdt'])+dec(pe['margin_usdt'])-exit_fees
    required=sum((dec(q['requested'])*dec(q['contract_size'])*dec(q['limit_price'] if q['side']=='buy' else q['best'])*(1+dec(state['fees'][q['kind']])) for q in new['quotes'][:2]),dec(0))
    if required>released:raise ValueError('SWITCH_EXCEEDS_RELEASED_CAPITAL')
    # 双方案都在同一窗口末退出，按当前盘口不变场景估未来退出：旧仓现在和未来
    # 的退出成本相消。新仓 F10 净收益已扣新开仓及未来退出，不能再重复扣这两笔。
    spmid=(dec(old['quotes'][0]['best'])+dec(spout['best']))/2
    pemid=(dec(old['quotes'][1]['best'])+dec(peout['best']))/2
    old_exit=dec(sp['quantity'])*spmid-dec(spout['notional_usdt'])+dec(peout['notional_usdt'])-dec(pe['quantity'])*dec(pe['contract_size'])*pemid+exit_fees
    old_income=dec(old['expected_funding_usdt']);new_income=dec(new['expected_funding_usdt'])
    incremental=old_exit+dec(new['roundtrip_cost_usdt'])-old_exit
    advantage=new_income-old_income-incremental
    return {'old_funding_usdt':str(old_income),'new_funding_usdt':str(new_income),
            'old_exit_now_usdt':str(old_exit),'old_terminal_exit_credit_usdt':str(old_exit),
            'new_roundtrip_usdt':new['roundtrip_cost_usdt'],'incremental_cost_usdt':str(incremental),
            'advantage_usdt':str(advantage),'buffer_usdt':str(dec(buffer)),
            'released_capital_usdt':str(released),'new_reserved_capital_usdt':str(required),
            'hold_hours':old['hold_hours'],'basis':'SAME_WINDOW_TERMINAL_LIQUIDATION_UNCHANGED_BOOKS_UNUSED_CASH_ZERO_RETURN',
            'eligible':new['status']=='CANDIDATE' and advantage>dec(buffer)}


def actual_released_capital(trade,rotation):
    baseline=rotation['exit_baseline'];spot=dec(0);perp=dec(0);cash=dec(0)
    orders=trade['orders'][baseline['order_count']:]
    for order in orders:
        q=order['quote'];amount=dec(q['filled']);cost=dec(q['notional_usdt']);fee=dec(q['fee_usdt'])
        if q['kind']=='spot' and q['side']=='sell':spot+=amount;cash+=cost-fee
        elif q['kind']=='perp' and q['side']=='buy':perp+=amount;cash-=cost+fee
        else:raise ValueError('UNEXPECTED_ROTATION_EXIT_ORDER')
    if spot!=dec(baseline['spot_quantity']) or perp!=dec(baseline['perp_quantity']):raise ValueError('ROTATION_EXIT_QUANTITY_UNCONFIRMED')
    return cash+dec(baseline['perp_entry_usdt'])+dec(baseline['perp_margin_usdt'])


def store(state,path,new):
    save(path,new);state.clear();state.update(new)


def run(state,path,ex,ranking,holdings,limits,settings,scan_started_ms,switch_rankings=None):
    settings=policy(settings);now=time.time();new=copy.deepcopy(state)
    if new.get('switch_policy') not in (None,settings):raise ValueError('SWITCH_POLICY_CHANGE_REQUIRES_REVIEW')
    new['switch_policy']=settings
    rotations=new.setdefault('rotations',[])
    current=next((r for r in rotations if r['phase'] not in ('DONE','CASH')),None)
    def finish(r,phase,reason):
        r.update(phase=phase,finished_at=now,reason=reason)
        new['switch_cash_until']=now+float(settings['cooldown_hours'])*3600 if phase=='CASH' else 0
        store(state,path,new);return {'action':'WAIT','reason':reason,'rotation':r}
    if current:
        old=new['trades'][current['old_trade_id']]
        if old['phase']!='closed' or any(dec(new['positions'].get(s,{}).get('quantity','0'))!=0 for s in (f"{old['coin']}/USDT",f"{old['coin']}/USDT:USDT")):
            return {'action':'WAIT','reason':'OLD_POSITION_NOT_FLAT'}
        if current['phase']=='OPENING':
            a=new.get('allocations',{}).get(current.get('allocation_id'),{})
            matches=[t for t in new['trades'].values() if current.get('allocation_id') and t.get('allocation_id')==current['allocation_id']]
            if len(matches)==1 and matches[0]['phase']=='open':return finish(current,'DONE','SWITCH_COMPLETED')
            if a.get('status')=='EXECUTING' or any(t['phase']!='closed' for t in matches):return {'action':'WAIT','reason':'NEW_POSITION_REQUIRES_RECOVERY'}
            return finish(current,'CASH','NEW_OPEN_NOT_CONFIRMED')
        if current['phase']=='EXITING':
            try:actual=actual_released_capital(old,current)
            except (KeyError,ValueError,TypeError,ArithmeticError):return finish(current,'CASH','EXIT_PROCEEDS_UNCONFIRMED')
            current.update(phase='WAIT_REQUOTE',flat_confirmed_ms=int(now*1000),actual_released_capital_usdt=str(actual))
            store(state,path,new)
            return {'action':'WAIT','reason':'REQUOTE_AFTER_CONFIRMED_FLAT'}
        if scan_started_ms<=current['flat_confirmed_ms']:return {'action':'WAIT','reason':'WAIT_POST_CLOSE_SCAN'}
        row=next((r for r in ranking['rows'] if r['coin']==current['new_coin'] and r['status']=='CANDIDATE'),None)
        if row is None:return finish(current,'CASH','OPPORTUNITY_DISAPPEARED')
        try:
            fresh(row)
            required=sum((dec(q['requested'])*dec(q['contract_size'])*dec(q['limit_price'] if q['side']=='buy' else q['best'])*(1+dec(state['fees'][q['kind']])) for q in row['quotes'][:2]),dec(0))
            if required>min(dec(current['economics']['released_capital_usdt']),dec(current['actual_released_capital_usdt'])):raise ValueError('NEW_CAPITAL_CHANGED')
            if dec(row['hold_hours'])!=dec(current['economics']['hold_hours']) or dec(row['expected_net_usdt'])<=dec(current['economics']['old_funding_usdt'])+dec(settings['buffer_usdt']):raise ValueError('NEW_NET_INSUFFICIENT')
        except (ValueError,KeyError,TypeError,ArithmeticError):return finish(current,'CASH','REQUOTE_NOT_ELIGIBLE')
        current['phase']='OPENING';store(state,path,new)
        decisions=allocate_a.reserve_batch(state,path,{'rows':[row]},limits,owner='paper_loop')
        if decisions[0]['status']!='RESERVED':
            new=copy.deepcopy(state);current=new['rotations'][-1];return finish(current,'CASH','NEW_ALLOCATION_REJECTED')
        ident=decisions[0]['reservation_id'];new=copy.deepcopy(state);new['rotations'][-1]['allocation_id']=ident;store(state,path,new)
        try:
            result=allocate_a.execute(state,path,ex,ident)
        except Exception:
            # execute 已保存实际状态；下一循环先恢复，再判断是否成功，不重放。
            return {'action':'WAIT','reason':'NEW_EXECUTION_REQUIRES_RECOVERY'}
        new=copy.deepcopy(state);current=new['rotations'][-1]
        if result['phase']=='open':return finish(current,'DONE','SWITCH_COMPLETED')
        return {'action':'WAIT','reason':'NEW_EXECUTION_REQUIRES_RECOVERY'}
    if now<new.get('switch_cash_until',0):return {'action':'WAIT','reason':'SWITCH_CASH_COOLDOWN'}
    recent=[r for r in rotations if now-r.get('finished_at',r['started_at'])<float(settings['cooldown_hours'])*3600]
    today=sum(int(r['started_at']//86400)==int(now//86400) for r in rotations)
    candidates=[];gaps=[]
    if not recent and today<settings['max_daily']:
        held_coins={t['coin'] for t in new['trades'].values() if t['phase']!='closed'}
        held_coins.update(a['coin'] for a in new.get('allocations',{}).values() if a['status'] in ('RESERVED','EXECUTING'))
        for ident,t in new['trades'].items():
            if t['phase']!='open' or now-t['opened_at']<float(settings['min_hold_hours'])*3600:continue
            for row in (switch_rankings or {}).get(ident,ranking)['rows']:
                if row['coin'] in held_coins or row['status']!='CANDIDATE':continue
                try:
                    e=economics(new,t,holdings[ident],row,settings['buffer_usdt'])
                    if e['eligible']:candidates.append((ident,row['coin'],e))
                except (ValueError,KeyError,TypeError,ArithmeticError) as exc:gaps.append(str(exc))
    candidates.sort(key=lambda c:(-dec(c[2]['advantage_usdt']),c[0],c[1]))
    if not candidates:
        new.pop('switch_confirmation',None);store(state,path,new)
        return {'action':'NONE','reason':'NO_CONFIRMED_INCREMENTAL_ADVANTAGE','gaps':gaps}
    ident,coin,e=candidates[0];key=f'{ident}:{coin}';prior=new.get('switch_confirmation',{})
    selected=next(r for r in (switch_rankings or {}).get(ident,ranking)['rows'] if r['coin']==coin)
    stamp=min([int(holdings[ident]['computed_at_ms']),int(selected['computed_at_ms'])]+[int(q['book_timestamp']) for row in (holdings[ident],selected) for q in row['quotes']])
    interval=float(settings['confirm_seconds'])
    if prior.get('key')!=key or now-prior.get('at',0)>interval*2+5:
        prior={'key':key,'count':1,'at':now,'stamp':stamp}
    elif stamp>prior['stamp'] and now-prior['at']>=interval:
        prior.update(count=prior['count']+1,at=now,stamp=stamp)
    new['switch_confirmation']=prior
    if prior['count']<settings['confirmations']:
        store(state,path,new);return {'action':'NONE','reason':'WAIT_CONSECUTIVE_CONFIRMATIONS','confirmation':prior,'economics':e}
    r={'id':uuid.uuid4().hex,'old_trade_id':ident,'new_coin':coin,'phase':'EXITING','started_at':now,'economics':e}
    old_trade=new['trades'][ident];sp=new['positions'][f"{old_trade['coin']}/USDT"];pe=new['positions'][f"{old_trade['coin']}/USDT:USDT"]
    r['exit_baseline']={'order_count':len(old_trade['orders']),'spot_quantity':sp['quantity'],'perp_quantity':pe['quantity'],'perp_entry_usdt':pe['entry_notional_usdt'],'perp_margin_usdt':pe['margin_usdt']}
    rotations.append(r);new.pop('switch_confirmation',None)
    new['trades'][ident]['exit_request']={'reason':'ROTATION','requested_at':now,'priority':1,'rotation_id':r['id']}
    store(state,path,new);return {'action':'WAIT','reason':'SWITCH_EXIT_REQUESTED','rotation':r}
