"""F13 paper 持有/退出；不放宽滑点，不推测未知订单，不调用真实交易。"""
import copy
import json
import time
from accounting import dec
from notify_events import save
import paper_a


def decide(trade,report,max_hold_hours=None):
    reasons=report.get('reasons',[])
    result={'trade_id':trade['trade_id'],'coin':trade['coin'],'action':'BLOCK','priority':9,'reason':None}
    now=time.time()
    if report.get('data_status')!='COMPLETE' or report.get('gaps'):
        return result|{'reason':'INCOMPLETE_DATA'}
    if not 0<=now-report.get('completed_at',0)<=5:
        return result|{'reason':'STALE_CHECK'}
    if any(not 0<=now-ts<=5 for ts in report.get('evidence_at',{}).values()):
        return result|{'reason':'STALE_EVIDENCE'}
    for leg in ('spot_execution','perp_execution'):
        book=report.get('metrics',{}).get(leg)
        if book and not 0<=now*1000-book['book_timestamp']<=5000:
            return result|{'reason':'STALE_BOOK'}
    if report.get('execution',{}).get('status')!='READY':
        return result|{'reason':'EXECUTION_BLOCKED'}
    if report.get('risk',{}).get('scope')!='local_and_public':
        return result|{'reason':'UNSUPPORTED_PAPER_RISK_SCOPE'}
    # 当前 paper 无维持保证金/强平模型，不把真实账户风险套在模拟仓位上。
    if any(r not in ('EXPOSURE_LIMIT','FUNDING_NEGATIVE','INCOMPLETE_EXECUTION') for r in reasons):
        return result|{'reason':'UNSUPPORTED_RISK_REASON'}
    if 'EXPOSURE_LIMIT' in reasons:return result|{'action':'EXIT','priority':0,'reason':'EXPOSURE_LIMIT'}
    if trade.get('exit_request'):return result|{'action':'EXIT','priority':1,'reason':'CONTINUE_CONFIRMED_EXIT'}
    if 'INCOMPLETE_EXECUTION' in reasons:return result|{'reason':'UNOWNED_INCOMPLETE_EXECUTION'}
    if 'FUNDING_NEGATIVE' in reasons:return result|{'action':'EXIT','priority':2,'reason':'FUNDING_NEGATIVE'}
    if max_hold_hours is not None:
        hours=dec(max_hold_hours)
        if hours<=0:raise ValueError('INVALID_MAX_HOLD_HOURS')
        age=dec(now)-dec(trade['opened_at'])
        if age<0:return result|{'reason':'TRADE_TIME_FUTURE'}
        if age>=hours*3600:return result|{'action':'EXIT','priority':3,'reason':'MAX_HOLD_REACHED'}
    return result|{'action':'HOLD','reason':'NO_EXIT_TRIGGER'}


def confirmed_flat(state,trade):
    if not trade.get('exit_request') or trade['phase'] not in ('closing','needs_close'):return False
    symbols={f"{trade['coin']}/USDT",f"{trade['coin']}/USDT:USDT"}
    if any(dec(state['positions'].get(s,{}).get('quantity','0'))!=0 for s in symbols):return False
    totals={s:dec(0) for s in symbols};seen=set();filled=False
    for order in trade['orders']:
        q=order['quote'];symbol=q['symbol']
        if symbol not in symbols or not order.get('simulated') or order.get('trade_id')!=trade['trade_id']:return False
        if order['order_id'] in seen or not order.get('executed_at_ms'):return False
        seen.add(order['order_id'])
        if q.get('status') not in ('FILLED','PARTIAL_CANCELED','CANCELED') or q['side'] not in ('buy','sell'):return False
        qty=dec(q['filled'])
        if qty<0:return False
        filled=filled or qty>0
        sign=1 if (symbol.endswith(':USDT') and q['side']=='sell') or (not symbol.endswith(':USDT') and q['side']=='buy') else -1
        totals[symbol]+=sign*qty
        if totals[symbol]<0:return False
    return filled and all(x==0 for x in totals.values())


def run(state,path,ex,reports,max_hold_hours=None):
    if state.get('mode')!='paper' or state.get('version')!=1:raise ValueError('INVALID_PAPER_STATE')
    if state.get('pending') or any(a['status']=='EXECUTING' for a in state.get('allocations',{}).values()):
        return [{'action':'BLOCK','reason':'UNKNOWN_EXECUTION'}]
    finalized=[]
    for ident,trade in list(state['trades'].items()):
        if confirmed_flat(state,trade):
            new=copy.deepcopy(state)
            new['trades'][ident].update(phase='closed',closed_at=max(o['executed_at_ms'] for o in trade['orders'])/1000,finalized_at=time.time())
            save(path,new);state.clear();state.update(new)
            finalized.append({'trade_id':ident,'coin':trade['coin'],'action':'EXIT','priority':1,'reason':'CONFIRMED_FLAT_FINALIZED','result':'CLOSED'})
    decisions=[decide(t,reports.get(t['trade_id'],{}),max_hold_hours) for t in state['trades'].values() if t['phase']!='closed']
    decisions.sort(key=lambda x:(x['priority'],x['coin']))
    for decision in decisions:
        if decision['action']!='EXIT':continue
        ident=decision['trade_id'];trade=state['trades'][ident]
        fresh=decide(trade,reports.get(ident,{}),max_hold_hours)
        if fresh['action']!='EXIT':decision.update(fresh);continue
        new=copy.deepcopy(state)
        new['trades'][ident].setdefault('exit_request',{'requested_at':time.time(),'reason':decision['reason'],'priority':decision['priority']})
        # 已知未执行的本循环预留可释放以退出；手工预留及未知状态不碰。
        for a in new.get('allocations',{}).values():
            if a.get('owner')=='paper_loop' and a['status']=='RESERVED':a['status']='RELEASED'
        save(path,new);state.clear();state.update(new)
        try:
            closed=paper_a.operate(state,path,ex,decision['coin'],closing=True)
            decision['result']='CLOSED' if closed['phase']=='closed' else 'PARTIAL_REQUIRES_NEXT_CHECK'
        except Exception as exc:
            state.clear();state.update(json.loads(path.read_text()))
            decision['result']='FAILED_REQUIRES_CHECK';decision['error']=type(exc).__name__
        if state.get('pending'):break
    return finalized+decisions
