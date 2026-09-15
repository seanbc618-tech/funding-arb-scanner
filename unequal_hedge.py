"""独立公共行情前向实验；无账户凭据、无真实订单，固定五组与因果止损。"""
import argparse
import copy
import decimal
import fcntl
import hashlib
import json
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from pathlib import Path

import ccxt
import paper_a as paper
import paper_funding as funding
from accounting import dec
from notify_events import save
from paper_replay import ReplayExchange

CONFIG = dict(ratios=['1','1.25','1.5','2','3'], coins=['BTC','ETH'], cash='20000',
              notional='2000', spot_fee='0.001', perp_fee='0.0005', hours=168,
              poll_seconds=2, max_gap_ms=15000, candle_age_ms=150000,
              atr_period=14, fast=20, slow=60, atr_multiple='2', min_stop_fraction='0.002',
              max_trade_risk='40', daily_loss='100', total_loss='400', cooldown_ms=1800000,
              max_hold_ms=21600000, max_daily_entries=4, buffer='1000')

V2_CONFIG={**CONFIG,'strategy':'v2','candle_bar':'5m','candle_limit':300,'candle_age_ms':450000}
V21_CONFIG={**V2_CONFIG,'collector_revision':'parallel-v1','display_version':'V2.1'}

def stamp(): return int(time.time()*1000)
def day(at): return datetime.fromtimestamp(at/1000,timezone.utc).date().isoformat()
def digest(x): return hashlib.sha256(json.dumps(x,sort_keys=True).encode()).hexdigest()
def write_once(path,x):
    with Path(path).open('x') as f: json.dump(x,f,ensure_ascii=False)
def position(s,sym): return dec(s['positions'].get(sym,{}).get('quantity','0'))
def log(s,event): s['events'].append(event)

def signals(rows,now,cfg):
    if cfg.get('strategy')=='v2':
        from unequal_signals_v2 import signals as v2_signals
        return v2_signals(rows,now,cfg)
    bars=sorted([r for r in rows if str(r[-1])=='1' and int(r[0])+60000<=now],key=lambda r:int(r[0]))[-(cfg['slow']+2):]
    if len(bars)<cfg['slow']+2 or not 0<=now-(int(bars[-1][0])+60000)<=cfg['candle_age_ms']:raise ValueError('CANDLE_HISTORY_GAP')
    if any(int(b[0])-int(a[0])!=60000 for a,b in zip(bars,bars[1:])):raise ValueError('CANDLE_INTERVAL_GAP')
    closes=[];ranges=[]
    for b in bars:
        o,h,l,c=map(dec,b[1:5])
        if not 0<l<=min(o,c)<=max(o,c)<=h:raise ValueError('INVALID_CANDLE')
        ranges.append(max(h-l,abs(h-closes[-1]),abs(l-closes[-1])) if closes else h-l);closes.append(c)
    def down(cs):
        return cs[-1]<sum(cs[-cfg['fast']:])/cfg['fast']<sum(cs[-cfg['slow']:])/cfg['slow']
    return {'at':int(bars[-1][0])+60000,'short':down(closes) and down(closes[:-1]),
            'atr':str(sum(ranges[-cfg['atr_period']:])/cfg['atr_period'])}

def initial(cfg,now):
    groups={}
    for ratio in cfg['ratios']:
        a=paper.initial(cfg['cash'],cfg['spot_fee'],cfg['perp_fee'])
        a.update(events=[],meta={},pending_actions={},funding={},halted=False,day=day(now),day_anchor='0',peak='0',drawdown='0')
        for coin in cfg['coins']:
            a['trades'][coin]={'orders':[]}
            a['meta'][coin]={'base_contracts':'0','extra':'0','cooldown_until':0,'entries':0,'entry_day':day(now)}
        groups[ratio]=a
    return dict(version=1,mode='INDEPENDENT_PUBLIC_PAPER',config=cfg,started_at_ms=now,last_at_ms=None,
                groups=groups,initialized=False,finished=False,coverage_gaps=[],expected={},last_frame=None)

def fill(a,ex,coin,symbol,side,qty,at,reason):
    q=paper.quote(ex,symbol,side,qty,now=at)
    if side=='buy' and q['kind']=='perp' and dec(q['filled'])>position(a,symbol):raise ValueError('OVERCLOSE')
    paper.apply_fill(a,q)
    order={'order_id':str(len(a['events'])),'trade_id':coin,'simulated':True,'executed_at_ms':at,'quote':q}
    a['trades'][coin]['orders'].append(order)
    log(a,dict(type='FILL',at_ms=at,reason=reason,order=order))
    return q

def value(a,ex,at):
    equity=dec(a['cash_usdt']);liquidation=equity;complete=True
    for sym,p in a['positions'].items():
        qty=dec(p['quantity'])
        if not qty:continue
        q=paper.quote(ex,sym,'sell' if p['kind']=='spot' else 'buy',qty,now=at)
        marked=qty*dec(p['contract_size'])*dec(q['best'])
        equity+=marked if p['kind']=='spot' else dec(p['entry_notional_usdt'])-marked
        if q['status']!='FILLED':complete=False;continue
        amount=dec(q['notional_usdt'])
        liquidation+=(amount if p['kind']=='spot' else dec(p['entry_notional_usdt'])-amount)-amount*dec(a['fees'][p['kind']])
    return equity, liquidation if complete else None

def queue_action(a,coin,kind,at,**kw):
    a['pending_actions'][coin]=dict(kind=kind,at_ms=at,**kw)
    log(a,dict(type='SIGNAL',coin=coin,at_ms=at,action=a['pending_actions'][coin].copy()))

def settle_all(s,frame,ex):
    now=frame['at_ms']
    for coin in s['config']['coins']:
        sym=coin+'/USDT:USDT';inst=ex.markets[sym]['id']
        rows=frame['history'].get(inst,[])
        if not rows or min(int(r['fundingTime']) for r in rows)>s['started_at_ms']:raise ValueError('FUNDING_HISTORY_TRUNCATED')
        current=frame['funding'][sym]
        s['expected'][sym]=sorted(set(s['expected'].get(sym,[])+[int(current['fundingTimestamp'])]+[int(r['fundingTime']) for r in rows if int(r['fundingTime'])>=s['started_at_ms']]))
        for at in s['expected'][sym]:
            if at<s['started_at_ms'] or at>now-60000:continue
            key=sym+':'+str(at)
            for a in s['groups'].values():
                if a['funding'].get(key,{}).get('status')=='POSTED':continue
                base=funding.held_base(a,sym,at,now)
                if not base:
                    a['funding'][key]={'status':'POSTED','payment_usdt':'0','at_ms':at};continue
                try:
                    rate_row,rate=funding.public_cycle(ex,sym,at)
                    candle,price=funding.public_price(ex,sym,at)
                    payment=base*price*rate
                    a['cash_usdt']=str(dec(a['cash_usdt'])+payment)
                    a['funding'][key]=dict(status='POSTED',payment_usdt=str(payment),at_ms=at,base=str(base),rate=rate_row,price=candle)
                    log(a,dict(type='FUNDING',key=key,at_ms=now,payment_usdt=str(payment)))
                except (ValueError,KeyError,TypeError,ArithmeticError) as err:
                    a['funding'][key]=dict(status='INVALID_DECLARED_GAP',payment_usdt=None,at_ms=at,gap=str(err))

def audit(s):
    with decimal.localcontext() as context:
        context.rounding=decimal.ROUND_HALF_UP
        return _audit(s)

def _audit(s):
    cfg=s['config']
    if list(s['groups'])!=cfg['ratios']:raise ValueError('GROUPS_CHANGED')
    for a in s['groups'].values():
        rebuilt=paper.initial(cfg['cash'],cfg['spot_fee'],cfg['perp_fee'])
        posted=set()
        for e in a['events']:
            if e['type']=='FILL':paper.apply_fill(rebuilt,copy.deepcopy(e['order']['quote']))
            elif e['type']=='FUNDING':
                if e['key'] in posted or dec(e['payment_usdt'])!=dec(a['funding'][e['key']]['payment_usdt']):raise ValueError('FUNDING_EVENT_MISMATCH')
                posted.add(e['key']);rebuilt['cash_usdt']=str(dec(rebuilt['cash_usdt'])+dec(e['payment_usdt']))
        if any(e['status']=='POSTED' and dec(e['payment_usdt']) and k not in posted for k,e in a['funding'].items()):raise ValueError('FUNDING_EVENT_MISSING')
        if dec(rebuilt['cash_usdt'])!=dec(a['cash_usdt']) or rebuilt['positions']!=a['positions']:raise ValueError('LEDGER_REPLAY_MISMATCH')
        for coin in cfg['coins']:
            sym=coin+'/USDT:USDT'
            funding.held_base(a,sym,0,s['last_at_ms'] or s['started_at_ms'])
            if not 0<=dec(a['meta'][coin]['extra'])<=position(a,sym):raise ValueError('OVERLAY_POSITION_MISMATCH')


def advance(s,frame,markets,precision):
    cfg=s['config'];now=frame['at_ms']
    if s['last_at_ms'] is not None and now<=s['last_at_ms']:raise ValueError('NONMONOTONIC_FRAME')
    delayed=s['last_at_ms'] is not None and now-s['last_at_ms']>cfg['max_gap_ms']
    if delayed:s['coverage_gaps'].append(dict(at_ms=now,gap='OBSERVATION_TIME_GAP'))
    delayed=delayed or s.pop('force_gap_exit',False)
    s['last_at_ms']=now
    ex=ReplayExchange({'markets':markets,'precision_mode':precision},frame)
    if frame.get('gap'):raise ValueError(frame['gap'])
    # 全组共用一批盘口，成交只能使用当前帧；信号至少下一帧才执行。
    for coin in cfg['coins']:
        for sym in (coin+'/USDT',coin+'/USDT:USDT'):
            b=frame['books'][sym]
            if not 0<=now-int(b['timestamp'])<=5000:raise ValueError('BOOK_TIME_GAP')
    if not s['initialized']:
        plans=[]
        for coin in cfg['coins']:
            sp,pe=coin+'/USDT',coin+'/USDT:USDT';size=dec(markets[pe]['contractSize'])
            contracts=paper.quantity(ex,pe,dec(cfg['notional'])/dec(frame['books'][sp]['asks'][0][0])/size)
            qty=paper.quantity(ex,sp,contracts*size)
            if qty!=contracts*size:raise ValueError('BASE_PRECISION_MISMATCH')
            for sym,side,q in ((sp,'buy',qty),(pe,'sell',contracts)):
                if paper.quote(ex,sym,side,q,now)['status']!='FILLED':raise ValueError('BASE_DEPTH_GAP')
                plans.append((coin,sym,side,q))
        for a in s['groups'].values():
            for coin,sym,side,q in plans:fill(a,ex,coin,sym,side,q,now,'COMMON_BASELINE')
            for coin in cfg['coins']:a['meta'][coin]['base_contracts']=str(position(a,coin+'/USDT:USDT'))
            if paper.available(a)<dec(cfg['buffer']):raise ValueError('BASE_CAPITAL')
        s['initialized']=True
    settle_all(s,frame,ex)
    sig={};signal_gaps={}
    for coin in cfg['coins']:
        try:sig[coin]=signals(frame['candles'][coin],now,cfg)
        except (ValueError,KeyError,TypeError,ArithmeticError) as err:signal_gaps[coin]=str(err)
    ending=now>=s['started_at_ms']+cfg['hours']*3600000
    base_equity,_=value(s['groups']['1'],ex,now)
    for ratio,a in s['groups'].items():
        equity,net=value(a,ex,now);increment=equity-base_equity
        if a['day']!=day(now):a.update(day=day(now),day_anchor=str(increment),daily_halted=False)
        if increment<=-dec(cfg['total_loss']):a['halted']=True
        if increment-dec(a['day_anchor'])<=-dec(cfg['daily_loss']):a['daily_halted']=True
        blocked=a['halted'] or a.get('daily_halted',False) or ending or any(e['status']!='POSTED' for e in a['funding'].values())
        for coin in cfg['coins']:
            m=a['meta'][coin];sp,pe=coin+'/USDT',coin+'/USDT:USDT'
            size=dec(markets[pe]['contractSize']);ask=dec(frame['books'][pe]['asks'][0][0])
            if m['entry_day']!=day(now):m.update(entry_day=day(now),entries=0)
            v2=cfg.get('strategy')=='v2'
            if v2 and coin in sig:
                from unequal_signals_v2 import rearm,economics
                rearm(m,sig[coin])
            action=a['pending_actions'].get(coin)
            if action and now>action['at_ms']:
                if action['kind']=='OPEN':
                    allowed=(not blocked and not delayed and coin in sig and sig[coin]['short'] and now-action['at_ms']<=cfg['max_gap_ms'] and dec(frame['funding'][pe]['fundingRate'])>0 and int(frame['funding'][pe]['fundingTimestamp'])>now)
                    if v2 and allowed:
                        allowed=sig[coin]['at']==action['signal_at'] and m.get('armed',True) and economics(sig[coin],frame['books'][pe]['bids'][0][0],ask,cfg)['pass']
                    a['pending_actions'].pop(coin)
                    if allowed:
                        qty=dec(action['quantity']);q=paper.quote(ex,pe,'sell',qty,now)
                        distance=dec(action['distance']);entry=dec(q['vwap']) if q['vwap'] else ask
                        risk=qty*size*(distance+entry*(2*dec(cfg['perp_fee'])+dec('0.001')))
                        cost_ok=True
                        if v2:
                            closing=paper.quote(ex,pe,'buy',qty,now)
                            cost_ok=q['status']=='FILLED' and closing['status']=='FILLED'
                            if cost_ok:
                                evidence=economics(sig[coin],q['vwap'],closing['vwap'],cfg)
                                m['entry_evidence']=dict(signal=sig[coin],economics=evidence,entry_quote=q,exit_quote=closing)
                                cost_ok=evidence['pass']
                        if cost_ok and q['status']=='FILLED' and risk<=dec(cfg['max_trade_risk']) and paper.available(a)-dec(cfg['buffer'])>=dec(q['notional_usdt'])*(1+dec(cfg['perp_fee'])):
                            q=fill(a,ex,coin,pe,'sell',qty,now,'OVERLAY_OPEN')
                            if v2:m['armed']=False
                            m.update(extra=q['filled'],entry=q['vwap'],distance=str(distance),stop=str(entry+distance),best=str(ask),opened_at=now,entries=m['entries']+1)
                        else:
                            m['cooldown_until']=now+60000
                            log(a,dict(type='ENTRY_REJECTED',coin=coin,at_ms=now,reason='DEPTH_CAPITAL_OR_RISK'))
                else:
                    extra=dec(m['extra'])
                    if extra:
                        q=fill(a,ex,coin,pe,'buy',extra,now,action['reason'])
                        m['extra']=str(extra-dec(q['filled']))
                    if not dec(m['extra']):
                        a['pending_actions'].pop(coin);m['cooldown_until']=now+cfg['cooldown_ms'];m['last_exit_signal']=sig.get(coin,{}).get('at',now)
            extra=dec(m['extra'])
            if extra and coin not in a['pending_actions']:
                # 止损使用可买回的 ask；先检测旧止损，再收紧。绝不放宽。
                reason=None
                if ask>=dec(m['stop']):reason='STOP'
                elif blocked and (not v2 or a['halted'] or a.get('daily_halted',False) or ending):reason='RISK_OR_END'
                elif delayed or coin in signal_gaps:reason='DATA_GAP_EXIT'
                elif now-m['opened_at']>=cfg['max_hold_ms']:reason='TIME_EXIT'
                elif (sig[coin]['exit'] if v2 else not sig[coin]['short']):reason='TREND_EXIT'
                if reason:queue_action(a,coin,'CLOSE',now,reason=reason)
                else:
                    entry=dec(m['entry']);distance=dec(m['distance']);best=min(dec(m['best']),ask);stop=dec(m['stop'])
                    if entry-best>=distance:
                        breakeven=entry*(1-dec(cfg['perp_fee'])-dec('0.001'))/(1+dec(cfg['perp_fee']))
                        stop=min(stop,breakeven)
                    if entry-best>=2*distance:stop=min(stop,best+distance)
                    m.update(best=str(best),stop=str(stop))
            elif not extra and coin not in a['pending_actions'] and dec(ratio)>1 and not blocked and not delayed and coin in sig:
                signal=sig[coin]
                if signal['short'] and (not v2 or m.get('armed',True)) and now>=m['cooldown_until'] and signal['at']>m.get('last_exit_signal',0)+60000 and m['entries']<cfg['max_daily_entries']:
                    current=frame['funding'][pe]
                    if dec(current['fundingRate'])<=0 or int(current['fundingTimestamp'])<=now:continue
                    if v2:
                        evidence=economics(signal,frame['books'][pe]['bids'][0][0],ask,cfg)
                        m['entry_evidence']=dict(signal=signal,economics=evidence)
                        if not evidence['pass']:continue
                    qty=paper.quantity(ex,pe,dec(m['base_contracts'])*(dec(ratio)-1))
                    distance=max(dec(signal['atr'])*dec(cfg['atr_multiple']),ask*dec(cfg['min_stop_fraction']))
                    queue_action(a,coin,'OPEN',now,quantity=str(qty),distance=str(distance),signal_at=signal['at'])
            if ending and not dec(m['extra']) and coin not in a['pending_actions']:
                # 观察期结束清算实际剩余量；部分成交下帧继续，不假定止损价成交。
                for sym,side in ((pe,'buy'),(sp,'sell')):
                    qty=position(a,sym)
                    if qty:fill(a,ex,coin,sym,side,qty,now,'EXPERIMENT_END')
        equity,net=value(a,ex,now)
        a['peak']=str(max(dec(a['peak']),equity-dec(cfg['cash'])))
        a['drawdown']=str(max(dec(a['drawdown']),dec(a['peak'])-(equity-dec(cfg['cash']))))
    s['finished']=ending and all(not any(dec(p['quantity']) for p in a['positions'].values()) for a in s['groups'].values()) and all(e['status']=='POSTED' for a in s['groups'].values() for e in a['funding'].values()) and now>=s['started_at_ms']+cfg['hours']*3600000+120000
    return report(s,frame,ex,signal_gaps)

def report(s,frame,ex,signal_gaps=None):
    rows=[];baseline=None
    for ratio,a in s['groups'].items():
        equity,net=value(a,ex,frame['at_ms'])
        if ratio=='1':baseline=net
        gaps=[x for x in a['funding'].values() if x['status']!='POSTED']
        eligible=not s['coverage_gaps'] and not gaps and net is not None and baseline is not None
        rows.append(dict(ratio=ratio,equity=str(equity),liquidation_value=str(net) if net is not None else None,
                         net_pnl=str(net-dec(s['config']['cash'])) if eligible else None,
                         diagnostic_net_pnl=str(net-dec(s['config']['cash'])) if net is not None else None,
                         excess_vs_1x=str(net-baseline) if eligible else None,
                         max_drawdown=str(a['drawdown']),fees=str(sum((dec(e['order']['quote']['fee_usdt']) for e in a['events'] if e['type']=='FILL'),dec(0))),
                         funding=str(sum((dec(e['payment_usdt']) for e in a['funding'].values() if e['status']=='POSTED'),dec(0))),
                         overlay_entries=sum(e.get('reason')=='OVERLAY_OPEN' for e in a['events']),
                         stop_fills=sum(e.get('reason')=='STOP' for e in a['events']),
                         pending=a['pending_actions'],meta=a['meta'],funding_gaps=gaps,
                         realized_ratios={c:str(position(a,c+'/USDT:USDT')/dec(a['meta'][c]['base_contracts'])) for c in s['config']['coins']}))
    return dict(at_ms=frame['at_ms'],status='FINISHED' if s['finished'] else 'OBSERVING',
                strategy=s['config'].get('strategy','v1'),signals=sig_for_report(s,frame),scope='FORWARD_PUBLIC_DEPTH_SIMULATION_NOT_REAL_FILLS',coverage_gaps=s['coverage_gaps'],signal_gaps=signal_gaps or {},rows=rows)

def sig_for_report(s,frame):
    out={}
    for coin in s['config']['coins']:
        try:out[coin]=signals(frame['candles'][coin],frame['at_ms'],s['config'])
        except (ValueError,KeyError,TypeError,ArithmeticError) as exc:out[coin]={'gap':str(exc)}
    return out

class Collector:
    def __init__(self,ex,cfg):
        self.ex=ex;self.cfg=cfg;self.cached={};self.updated=0
        self.pool=ThreadPoolExecutor(max_workers=4)
        self.clients={}
        for coin in cfg['coins']:
            for sym in (coin+'/USDT',coin+'/USDT:USDT'):
                client=ccxt.okx({'timeout':5000})
                client.set_markets(ex.markets,ex.currencies)
                self.clients[sym]=client

    def request(self,ex,method,symbol,*args,**kwargs):
        try:return getattr(ex,method)(*args,**kwargs)
        except ccxt.BaseError as err:
            # Public endpoint and error class only; never persist request headers.
            raise ValueError(f'COLLECTOR:{symbol}:{method}:{type(err).__name__}') from err

    def refresh(self,coin,s,now):
        sym=coin+'/USDT:USDT';ex=self.clients[sym];inst=ex.markets[sym]['id']
        response=self.request(ex,'publicGetMarketCandles',sym,{'instId':inst,'bar':self.cfg.get('candle_bar','1m'),'limit':str(self.cfg.get('candle_limit',100))})
        if str(response.get('code'))!='0':raise ValueError('CANDLE_API_ERROR:'+sym)
        candles=response['data'];rate=self.request(ex,'fetch_funding_rate',sym,sym)
        response=self.request(ex,'publicGetPublicFundingRateHistory',sym,{'instId':inst,'limit':'100'})
        if str(response.get('code'))!='0':raise ValueError('FUNDING_API_ERROR:'+sym)
        history=response['data'];marks=[]
        for row in history:
            at=int(row['fundingTime']);key=sym+':'+str(at)
            if at<s['started_at_ms'] or at>now-60000:continue
            if all(a['funding'].get(key,{}).get('status')=='POSTED' for a in s['groups'].values()):continue
            response_price=self.request(ex,'publicGetMarketHistoryMarkPriceCandles',sym,{'instId':inst,'bar':'1m','before':str(at-1),'after':str(at+60000),'limit':'100'})
            if str(response_price.get('code'))!='0':raise ValueError('MARK_API_ERROR:'+sym)
            marks.extend(response_price['data'])
        return coin,sym,inst,candles,rate,history,marks

    def collect(self,jobs):
        futures=[self.pool.submit(fn,*args) for fn,args in jobs]
        # Drain every lane before returning or raising: a client is never used
        # by an old unfinished frame and a new frame concurrently.
        results=[];failure=None
        for future in futures:
            try:results.append(future.result())
            except Exception as err:
                if failure is None:failure=err
        if failure is not None:raise failure
        return results

    def frame(self,s):
        now=stamp();frame={'books':{},'at_ms':now}
        if now-self.updated>=30000 or not self.cached:
            data={'candles':{},'funding':{},'history':{},'mark_candles':{}}
            for coin,sym,inst,candles,rate,history,marks in self.collect([(self.refresh,(coin,s,now)) for coin in self.cfg['coins']]):
                data['candles'][coin]=candles;data['funding'][sym]=rate
                data['history'][inst]=history;data['mark_candles'][inst]=marks
            self.cached=data;self.updated=stamp()
        frame.update(copy.deepcopy(self.cached))
        symbols=list(self.clients)
        books=self.collect([(self.request,(self.clients[sym],'fetch_order_book',sym,sym,50)) for sym in symbols])
        frame['books']=dict(zip(symbols,books))
        frame['at_ms']=stamp();return frame

def run(directory,cycles=0,cfg=None):
    cfg=CONFIG if cfg is None else cfg
    decimal.getcontext().rounding=decimal.ROUND_HALF_UP
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=True)
    with (directory/'experiment.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        ex=ccxt.okx({'timeout':5000});ex.load_markets()
        names=sorted(p.name for p in Path(__file__).resolve().parent.glob('*.py'))
        hashes={n:hashlib.sha256(Path(__file__).with_name(n).read_bytes()).hexdigest() for n in names}
        path=directory/'state.json'
        if path.exists():
            contract=json.loads((directory/'contract.json').read_text());s=json.loads(path.read_text())
            if contract['source_hashes']!=hashes or contract['config']!=cfg or contract['ccxt_version']!=ccxt.__version__:raise ValueError('FROZEN_EXPERIMENT_CHANGED')
        else:
            if (directory/'contract.json').exists():raise ValueError('INITIALIZATION_INCOMPLETE')
            s=initial(copy.deepcopy(cfg),stamp())
            write_once(directory/'contract.json',dict(config=cfg,source_hashes=hashes,ccxt_version=ccxt.__version__,started_at_ms=s['started_at_ms'],markets=ex.markets,precision_mode=ex.precisionMode))
            save(path,s)
        audit(s)
        collector=Collector(ex,cfg);count=0
        while not s['finished'] and (not cycles or count<cycles):
            begun=time.monotonic();frame=None
            try:
                frame=collector.frame(s)
                frame_path=directory/('frame-'+str(frame['at_ms'])+'.json');write_once(frame_path,frame)
                new=copy.deepcopy(s);result=advance(new,frame,ex.markets,ex.precisionMode)
                new['last_frame']=frame_path.name;audit(new);save(path,new);s=new
            except (ValueError,KeyError,TypeError,ArithmeticError,ccxt.BaseError) as err:
                now=stamp();gap=str(err) if isinstance(err,ValueError) else type(err).__name__
                s['coverage_gaps'].append(dict(at_ms=now,gap=gap));s['force_gap_exit']=True;save(path,s)
                result=dict(at_ms=now,status='INVALID_DECLARED_GAP',gap=gap,rows=[],scope='NO_FABRICATED_FILL_OR_PRICE')
            save(directory/'report.json',result)
            print(json.dumps(dict(at_ms=result['at_ms'],status=result['status'],gap=result.get('gap'))),flush=True)
            count+=1;time.sleep(max(0,cfg['poll_seconds']-(time.monotonic()-begun)))

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',required=True);p.add_argument('--cycles',type=int,default=0)
    p.add_argument('--v2',action='store_true')
    p.add_argument('--v21',action='store_true')
    a=p.parse_args();run(a.directory,a.cycles,V21_CONFIG if a.v21 else V2_CONFIG if a.v2 else CONFIG)
if __name__=='__main__':main()
