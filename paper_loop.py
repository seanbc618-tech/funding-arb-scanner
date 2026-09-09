"""F12 有限或持续 paper 循环；扫描独立进程，主进程优先检查持仓。"""
import argparse
import fcntl
import json
import hashlib
import multiprocessing as mp
import os
from pathlib import Path
import queue
import time

from accounting import dec
from notify_events import save
import paper_a as paper
import allocate_a
import paper_funding
import rank_a
import check_a


def exchange(private=False):
    import ccxt
    from scan import PROXY
    cfg={'timeout':10000,**({'httpsProxy':PROXY} if PROXY else {})}
    if private:
        names={'apiKey':'OKX_API_KEY','secret':'OKX_SECRET','password':'OKX_PASSWORD'}
        if any(not os.environ.get(v) for v in names.values()):raise ValueError('CONFIG_MISSING')
        cfg.update({k:os.environ[v] for k,v in names.items()})
    ex=ccxt.okx(cfg);ex.load_markets();return ex


def collect_funding(ex,state):
    """发现已发生的真实周期；历史截断声明缺口，不假设 8 小时补周期。"""
    batches=[]
    symbols={o['quote']['symbol'] for t in state['trades'].values() for o in t['orders'] if o['quote']['kind']=='perp' and dec(o['quote']['filled'])>0}
    for symbol in sorted(symbols):
        item={'symbol':symbol,'cycles':[],'gap':None}
        try:
            orders=[o for t in state['trades'].values() for o in t['orders'] if o['quote']['symbol']==symbol and dec(o['quote']['filled'])>0]
            if any(o.get('executed_at_ms') is None for o in orders):raise ValueError('LEGACY_FILL_TIME_MISSING')
            start=min(o['executed_at_ms'] for o in orders);cursor=None;rows={};covered=False
            for _ in range(30):
                params={'instId':ex.markets[symbol]['id'],'limit':'100'}
                if cursor is not None:params['after']=str(cursor)
                response=ex.publicGetPublicFundingRateHistory(params)
                if str(response.get('code'))!='0' or not response['data']:break
                for row in response['data']:
                    ts=int(row['fundingTime'])
                    if row['instId']!=params['instId']:raise ValueError('WRONG_FUNDING_SYMBOL')
                    if ts in rows and rows[ts]!=row:raise ValueError('CONFLICTING_FUNDING')
                    rows[ts]=row
                oldest=min(int(r['fundingTime']) for r in response['data'])
                if oldest<=start:covered=True;break
                if cursor is not None and oldest>=cursor:raise ValueError('HISTORY_CURSOR_STALLED')
                cursor=oldest
            if not covered:raise ValueError('INVALID_DECLARED_GAP_HISTORY_TRUNCATED')
            now=int(time.time()*1000)
            for at,row in sorted(rows.items()):
                if at<start or at>now-60000:continue
                old=state.get('funding',{}).get(f'{symbol}:{at}',{})
                if old.get('status') in ('POSTED','NO_POSITION'):continue
                # F09 自己校验真实周期与行情；缓存原始返回避免主进程等待扫描请求。
                cycle={'at':at,'rate':{'code':'0','data':[row]},'price':{'code':'0','data':[]}}
                try:
                    candle,_=paper_funding.public_price(ex,symbol,at)
                    cycle['price']['data']=[candle]
                except Exception as exc:cycle['price_gap']=type(exc).__name__
                item['cycles'].append(cycle)
        except Exception as exc:item['gap']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
        batches.append(item)
    return batches


def identity(state):
    return hashlib.sha256(json.dumps({'trades':state['trades'],'positions':state['positions']},sort_keys=True).encode()).hexdigest()


def worker(out,stop,path,coins,scenario,interval):
    # 此进程只读取账本及行情/账户费率，绝不修改交易状态。
    while not stop.is_set():
        result={'started_at_ms':int(time.time()*1000),'ranking':None,'funding':[],'gap':None}
        try:
            public=exchange();state=json.loads(Path(path).read_text())
            result['state_identity']=identity(state)
            result['funding']=collect_funding(public,state)
            try:result['ranking']=rank_a.rank(exchange(True),coins,**scenario)
            except Exception as exc:result['gap']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__
        except Exception as exc:result['gap']=type(exc).__name__
        result['completed_at_ms']=int(time.time()*1000)
        try:out.put_nowait(result)
        except queue.Full:pass
        stop.wait(interval)


def drain(inbox):
    latest=None
    while True:
        try:latest=inbox.get_nowait()
        except queue.Empty:return latest


def reconcile(state):
    if state.get('mode')!='paper' or state.get('version')!=1:raise ValueError('INVALID_PAPER_STATE')
    if state.get('pending'):raise ValueError('PENDING_EXECUTION')
    if any(a['status']=='EXECUTING' for a in state.get('allocations',{}).values()):raise ValueError('ALLOCATION_EXECUTING')
    active=[t for t in state['trades'].values() if t['phase']!='closed']
    if any(t['phase']!='open' for t in active):raise ValueError('UNFINISHED_TRADE')
    if len({t['coin'] for t in active})!=len(active):raise ValueError('DUPLICATE_ACTIVE_COIN')
    totals={}
    for t in state['trades'].values():
        for o in t['orders']:
            q=o['quote'];symbol=q['symbol']
            sign=1 if (q['kind']=='spot' and q['side']=='buy') or (q['kind']=='perp' and q['side']=='sell') else -1
            totals[symbol]=totals.get(symbol,dec(0))+dec(q['filled'])*sign
    for symbol in set(totals)|set(state['positions']):
        qty=dec(state['positions'].get(symbol,{}).get('quantity','0'))
        if qty<0 or qty!=totals.get(symbol,dec(0)):raise ValueError('POSITION_HISTORY_MISMATCH')
        if qty>0 and symbol.split('/')[0] not in {t['coin'] for t in active}:raise ValueError('UNOWNED_PAPER_POSITION')
    return active


def check_positions(ex,state):
    reports={}
    for t in state['trades'].values():
        if t['phase']=='closed':continue
        coin=t['coin'];spot=f'{coin}/USDT';perp=f'{coin}/USDT:USDT'
        sp=state['positions'].get(spot,{});pe=state['positions'].get(perp,{})
        p={'spot':spot,'perp':perp,'base':sp.get('quantity','0'),'contracts':pe.get('quantity','0'),
           'size':pe.get('contract_size',ex.markets.get(perp,{}).get('contractSize')),'phase':t['phase'],'pending':state.get('pending')}
        try:reports[t['trade_id']]=check_a.check(ex,p,private=False)
        except Exception as exc:reports[t['trade_id']]={'action':'BLOCK','gap':type(exc).__name__}
    return reports


def positions_ready(reports):
    now=time.time()
    for r in reports.values():
        if r.get('action') not in ('HOLD','PASS') or r.get('data_status')!='COMPLETE':return False
        if not 0<=now-r.get('completed_at',0)<=5:return False
        if any(not 0<=now-ts<=5 for ts in r.get('evidence_at',{}).values()):return False
        for leg in ('spot_execution','perp_execution'):
            book=r.get('metrics',{}).get(leg)
            if book and not 0<=now*1000-book['book_timestamp']<=5000:return False
    return True


class CachedFunding:
    def __init__(self,ex,cycle):self.markets=ex.markets;self.cycle=cycle
    def publicGetPublicFundingRateHistory(self,params):return self.cycle['rate']
    def publicGetMarketHistoryMarkPriceCandles(self,params):return self.cycle['price']


def tick(path,ex,inbox,limits):
    """调用者必须持有账本锁。扫描读取为非阻塞，检查始终先于扫描结果消费。"""
    state=json.loads(Path(path).read_text());report={'started_at_ms':int(time.time()*1000),'steps':[],'decision':'BLOCK'}
    reports=check_positions(ex,state);report['positions']=reports;report['steps'].append('POSITIONS_CHECKED')
    try:reconcile(state)
    except Exception as exc:report['reconciliation_gap']=str(exc);return report
    report['steps'].append('RECONCILED')
    snapshot=drain(inbox)
    if snapshot is None:report['decision']='WAIT_SCAN';return report
    report['scan_started_at_ms']=snapshot['started_at_ms'];report['scan_completed_at_ms']=snapshot['completed_at_ms'];report['scan_gap']=snapshot.get('gap')
    if snapshot.get('state_identity')!=identity(state):
        report['decision']='STATE_CHANGED_RESCAN';return report
    report['funding']=[];funding_gap=False
    for batch in snapshot.get('funding',[]):
        if batch['gap']:funding_gap=True
        report['funding'].append({'symbol':batch['symbol'],'gap':batch['gap'],'results':[]})
        for cycle in batch['cycles']:
            entry=paper_funding.settle(state,path,CachedFunding(ex,cycle),batch['symbol'],cycle['at'])
            report['funding'][-1]['results'].append(entry)
            if entry['status']=='INVALID_DECLARED_GAP':funding_gap=True
    report['steps'].append('FUNDING_CHECKED')
    # 回放发现缺口或风险/退出建议时只报告，F13 才实现自动退出。
    if funding_gap or not positions_ready(reports):
        report['decision']='POSITION_OR_FUNDING_REVIEW';return report
    if snapshot.get('gap') or snapshot.get('ranking') is None:
        report['decision']='SCAN_UNAVAILABLE';return report
    if not 0<=time.time()*1000-snapshot['completed_at_ms']<=5000:
        report['decision']='STALE_SCAN';return report
    # 仅释放本循环明确创建且尚未执行的预留；不处理用户手工预留。
    for ident,a in list(state.get('allocations',{}).items()):
        if a.get('owner')=='paper_loop' and a['status']=='RESERVED':allocate_a.release(state,path,ident)
    decisions=allocate_a.reserve_batch(state,path,snapshot['ranking'],limits,owner='paper_loop')
    report['allocations']=decisions;report['executions']=[]
    for d in decisions:
        if d['status']!='RESERVED':continue
        ident=d['reservation_id']
        if not positions_ready(reports):
            report['execution_gap']='POSITION_CHECK_EXPIRED';break
        try:
            result=allocate_a.execute(state,path,ex,ident);report['executions'].append(result)
            if result['phase']!='open':break
        except Exception as exc:
            report['execution_gap']=str(exc) if isinstance(exc,ValueError) else type(exc).__name__;break
    for d in decisions:
        if d['status']=='RESERVED':
            ident=d['reservation_id']
            state=json.loads(Path(path).read_text())
            if state['allocations'][ident]['status']=='RESERVED':allocate_a.release(state,path,ident)
    report['available_usdt']=str(paper.available(state));report['decision']='PAPER_EXECUTED' if report['executions'] else 'HOLD_CASH'
    if report.get('execution_gap') or any(t['phase']!='open' for t in report['executions']):report['decision']='EXECUTION_REVIEW'
    report['steps'].append('ALLOCATION_AND_EXECUTION_CHECKED');return report


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('coins',nargs='+');parser.add_argument('--cycles',type=int,default=1,help='0 = continuous')
    parser.add_argument('--interval',type=float,default=1)
    for arg in ('notional','hold-hours','per-coin-gross-usdt','total-gross-usdt','max-positions','buffer-usdt','basis-stress-bps'):parser.add_argument('--'+arg,required=True)
    args=parser.parse_args()
    if args.cycles<0 or not 1<=args.interval<=60:parser.error('invalid cycles/interval')
    scenario=dict(notional=args.notional,hold_hours=args.hold_hours,margin_ratio=1,reserve_usdt=args.buffer_usdt,basis_stress_bps=args.basis_stress_bps)
    limits={k:getattr(args,k) for k in ('per_coin_gross_usdt','total_gross_usdt','max_positions','buffer_usdt')}
    path=paper.ROOT/'paper_a_state.json'
    # 独立进程锁阻止两套循环；账本锁仅覆盖每次主循环，不被扫描占用。
    with (paper.ROOT/'paper_loop.lock').open('a') as loop_lock:
        os.fchmod(loop_lock.fileno(),0o600);fcntl.flock(loop_lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        ctx=mp.get_context('spawn');inbox=ctx.Queue(maxsize=1);stop=ctx.Event()
        process=ctx.Process(target=worker,args=(inbox,stop,str(path),[c.upper() for c in args.coins],scenario,args.interval),daemon=True);process.start()
        ex=None;number=0;last_scan_success=None;last_position_check=None
        try:
            while args.cycles==0 or number<args.cycles:
                started=time.time();number+=1
                try:
                    if ex is None:ex=exchange()
                    with path.with_suffix('.lock').open('a') as lock:
                        os.fchmod(lock.fileno(),0o600);fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
                        report=tick(path,ex,inbox,limits)
                except Exception as exc:report={'decision':'BLOCK','gap':type(exc).__name__}
                if report.get('positions') is not None:last_position_check=int(time.time()*1000)
                if report.get('scan_completed_at_ms') and not report.get('scan_gap'):last_scan_success=report['scan_completed_at_ms']
                report.update(cycle=number,completed_at_ms=int(time.time()*1000),scanner_alive=process.is_alive(),
                              last_scan_success_ms=last_scan_success,last_position_check_ms=last_position_check)
                save(paper.ROOT/'paper_loop_status.json',report);print(json.dumps(report,ensure_ascii=False),flush=True)
                if args.cycles and number>=args.cycles:break
                time.sleep(max(0,args.interval-(time.time()-started)))
        except KeyboardInterrupt:pass
        finally:
            stop.set();process.join(timeout=1)
            if process.is_alive():process.terminate();process.join(timeout=1)
            inbox.close()

if __name__=='__main__':main()
