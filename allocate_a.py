"""F11 paper 组合预算与预留；名义上限按开仓成本计，不是动态风险额度。"""
import argparse
import copy
import fcntl
import json
import os
from pathlib import Path
import time
import uuid

from accounting import dec
from notify_events import save


def reserved(state):
    return sum((dec(x['capital_usdt']) for x in state.get('allocations',{}).values()
                if x['status']=='RESERVED'),dec(0))


def reserve_batch(state,path,ranking,limits,owner=None):
    from paper_a import available
    if state.get('mode')!='paper' or state.get('version')!=1:raise ValueError('INVALID_PAPER_STATE')
    per,total,buffer=map(dec,(limits['per_coin_gross_usdt'],limits['total_gross_usdt'],limits['buffer_usdt']))
    count=dec(limits['max_positions'])
    if per<=0 or total<=0 or buffer<0 or count<=0 or count!=int(count):raise ValueError('INVALID_LIMITS')
    limits={'per_coin_gross_usdt':str(per),'total_gross_usdt':str(total),'buffer_usdt':str(buffer),'max_positions':int(count)}
    if state.get('pending') or any(t['phase'] not in ('open','closed') for t in state['trades'].values()) or any(x['status']=='EXECUTING' for x in state.get('allocations',{}).values()):
        raise ValueError('UNFINISHED_EXECUTION')
    if state.get('allocation_limits') not in (None,limits):raise ValueError('LIMIT_CHANGE_REQUIRES_REVIEW')
    new=copy.deepcopy(state);new['allocation_limits']=limits
    allocations=new.setdefault('allocations',{})
    usage={}
    for symbol,p in new['positions'].items():
        amount=dec(p['entry_notional_usdt'])
        if amount<0 or dec(p['quantity'])<0:raise ValueError('INVALID_POSITION')
        if dec(p['quantity'])>0:
            coin=symbol.split('/')[0];usage[coin]=usage.get(coin,dec(0))+amount
    for t in new['trades'].values():
        if t['phase']!='closed':usage.setdefault(t['coin'],dec(0))
    for a in allocations.values():
        if a['status']=='RESERVED':usage[a['coin']]=usage.get(a['coin'],dec(0))+dec(a['gross_usdt'])
    if any(v>per for v in usage.values()) or sum(usage.values())>total or len(usage)>count:
        raise ValueError('EXISTING_USAGE_EXCEEDS_LIMITS')
    decisions=[]
    rows=sorted(ranking['rows'],key=lambda r:(r.get('rank') or 10**9,r['coin']))
    for row in rows:
        coin=row['coin'];decision={'coin':coin,'status':'SKIPPED','reason':None}
        try:
            if row['status']!='CANDIDATE' or not row.get('rank') or dec(row['expected_net_usdt'])<=0:raise ValueError('NOT_CANDIDATE')
            if coin in usage:raise ValueError('COIN_ALREADY_ALLOCATED')
            if len(usage)>=count:raise ValueError('MAX_POSITIONS')
            if dec(row['margin_ratio_assumption'])!=1:raise ValueError('PAPER_REQUIRES_1X_MARGIN')
            quotes=row['quotes'];expected=[(f'{coin}/USDT','buy'),(f'{coin}/USDT:USDT','sell'),(f'{coin}/USDT','sell'),(f'{coin}/USDT:USDT','buy')]
            if len(quotes)!=4 or [(q['symbol'],q['side']) for q in quotes]!=expected:raise ValueError('INVALID_QUOTES')
            now=time.time()*1000
            if dec(row['next_funding_ms'])<=dec(now):raise ValueError('FUNDING_BOUNDARY_CROSSED')
            if any(q['status']!='FILLED' or not 0<=now-q['book_timestamp']<=5000 for q in quotes):raise ValueError('STALE_OR_PARTIAL_QUOTES')
            leg_caps={};capital=dec(0)
            for q in quotes[:2]:
                amount=dec(q['requested'])*dec(q['contract_size'])*dec(q['limit_price'] if q['side']=='buy' else q['best'])
                if amount<=0:raise ValueError('INVALID_NOTIONAL')
                leg_caps[q['symbol']]=str(amount)
                fee=dec(new['fees'][q['kind']])
                if fee!=dec(row['fees'][q['symbol']]):raise ValueError('PAPER_AND_RANKING_FEES_DIFFER')
                capital+=amount*(1+fee)
            gross=sum(map(dec,leg_caps.values()))
            if gross>per:raise ValueError('PER_COIN_GROSS_LIMIT')
            if sum(usage.values())+gross>total:raise ValueError('TOTAL_GROSS_LIMIT')
            if available(new)-buffer<capital:raise ValueError('INSUFFICIENT_UNRESERVED_CAPITAL')
            ident=uuid.uuid4().hex
            allocations[ident]={'id':ident,'coin':coin,'status':'RESERVED','capital_usdt':str(capital),
                'gross_usdt':str(gross),'leg_caps':leg_caps,'notional':str(dec(quotes[0]['requested'])*dec(quotes[0]['best'])),
                'created_at_ms':int(now),'ranking':row,'owner':owner}
            usage[coin]=gross;decision.update(status='RESERVED',reservation_id=ident)
        except (ValueError,KeyError,TypeError,ArithmeticError) as exc:
            decision['reason']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
        decisions.append(decision)
    save(path,new);state.clear();state.update(new)
    return decisions


def release(state,path,ident):
    new=copy.deepcopy(state);a=new['allocations'][ident]
    if a['status']!='RESERVED':raise ValueError('RESERVATION_NOT_RELEASABLE')
    a['status']='RELEASED';save(path,new);state.clear();state.update(new)


def execute(state,path,ex,ident):
    from paper_a import operate
    if state.get('mode')!='paper' or state.get('version')!=1:raise ValueError('INVALID_PAPER_STATE')
    a=state['allocations'][ident]
    if a['status']!='RESERVED':raise ValueError('RESERVATION_NOT_EXECUTABLE')
    if not 0<=time.time()*1000-a['created_at_ms']<=5000:raise ValueError('RESERVATION_EXPIRED_RELEASE_AND_REQUOTE')
    if dec(a['ranking']['next_funding_ms'])<=dec(time.time()*1000):raise ValueError('FUNDING_BOUNDARY_CROSSED')
    if state.get('pending') or any(t['phase'] not in ('open','closed') for t in state['trades'].values()) or any(x['status']=='EXECUTING' for x in state['allocations'].values()):raise ValueError('UNFINISHED_EXECUTION')
    new=copy.deepcopy(state);new['allocations'][ident]['status']='EXECUTING'
    save(path,new);state.clear();state.update(new)
    try:
        result=operate(state,path,ex,a['coin'],a['notional'],allocation_id=ident)
    except Exception:
        persisted=json.loads(path.read_text());persisted['allocations'][ident]['status']='FAILED'
        save(path,persisted);state.clear();state.update(persisted);raise
    persisted=json.loads(path.read_text());persisted['allocations'][ident]['status']='FINISHED'
    persisted['allocations'][ident]['trade_id']=result['trade_id']
    save(path,persisted);state.clear();state.update(persisted)
    return result


def main():
    import paper_a
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=['reserve','execute','release'])
    parser.add_argument('target',help='ranking JSON path or reservation ID')
    parser.add_argument('--per-coin-gross-usdt');parser.add_argument('--total-gross-usdt')
    parser.add_argument('--max-positions');parser.add_argument('--buffer-usdt')
    args=parser.parse_args();path=paper_a.ROOT/'paper_a_state.json'
    with path.with_suffix('.lock').open('a') as lock:
        os.fchmod(lock.fileno(),0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        state=json.loads(path.read_text())
        if args.command=='reserve':
            limits={k:getattr(args,k) for k in ('per_coin_gross_usdt','total_gross_usdt','max_positions','buffer_usdt')}
            if any(v is None for v in limits.values()):parser.error('reserve requires all four limits')
            result=reserve_batch(state,path,json.loads(Path(args.target).read_text()),limits)
        elif args.command=='release':release(state,path,args.target);result={'released':args.target}
        else:
            import ccxt
            from scan import PROXY
            ex=ccxt.okx({'timeout':15000,**({'httpsProxy':PROXY} if PROXY else {})});ex.load_markets()
            result=execute(state,path,ex,args.target)
        print(json.dumps(result,ensure_ascii=False,indent=2))

if __name__=='__main__':main()
