"""F10 OKX 模式 A 净收益只读排名；预测不是入账，不产生执行队列。"""
import argparse
import copy
import json
import os
import time

from accounting import dec
from paper_a import quote, quantity, market
from check_a import MAX_BOOK_AGE_MS

DAY=86400000


def forecast(ex,symbol,now,hold_hours):
    current=ex.fetch_funding_rate(symbol)['info']
    if current.get('instId')!=ex.markets[symbol]['id'] or current.get('method')!='current_period':
        raise ValueError('UNSUPPORTED_FUNDING_CONTRACT_OR_METHOD')
    next_at=int(current['fundingTime']);following=int(current['nextFundingTime'])
    interval=following-next_at
    if interval<=0 or not now<next_at<=now+interval:
        raise ValueError('FUNDING_SCHEDULE_UNKNOWN_OR_STALE')
    rate=dec(current['fundingRate'])
    start=now-7*DAY;cursor=None;points={}
    for _ in range(30):
        params={'instId':ex.markets[symbol]['id'],'limit':'100'}
        if cursor is not None:params['after']=str(cursor)
        response=ex.publicGetPublicFundingRateHistory(params)
        if str(response.get('code'))!='0':raise ValueError('HISTORY_API_ERROR')
        rows=response['data']
        if not rows:break
        stamps=[]
        for r in rows:
            ts=int(r['fundingTime']);stamps.append(ts)
            if r['instId']!=params['instId'] or ts>now:raise ValueError('INVALID_HISTORY')
            val=dec(r['realizedRate'])
            if ts in points and points[ts]!=val:raise ValueError('CONFLICTING_HISTORY')
            points[ts]=val
        oldest=min(stamps)
        if cursor is not None and oldest>=cursor:raise ValueError('HISTORY_CURSOR_STALLED')
        if oldest<=start:break
        cursor=oldest
    pts=sorted((ts,r) for ts,r in points.items() if ts>=start)
    if len(pts)<2 or pts[0][0]>start+interval or now-pts[-1][0]>interval or pts[-1][0]>=next_at:
        raise ValueError('HISTORY_SHORT_OR_STALE')
    if next_at-pts[-1][0]!=interval or any(b[0]-a[0]!=interval for a,b in zip(pts,pts[1:])):
        raise ValueError('HISTORY_GAP_OR_INTERVAL_CHANGED')
    mean=sum((r for _,r in pts),dec(0))/len(pts)
    projected=min(rate,mean)
    end=dec(now)+dec(hold_hours)*3600000
    cycles=max(0,int((end-next_at)//interval)+1) if end>=next_at else 0
    return {'current_funding_evidence':{k:current[k] for k in ('instId','method','fundingTime','nextFundingTime','fundingRate')},
            'historical_rate_sum':str(sum((r for _,r in pts),dec(0))),
            'historical_mean_rate':str(mean),'historical_apr':str(mean*365*DAY/interval),
            'history_start_ms':pts[0][0],'history_end_ms':pts[-1][0],'history_count':len(pts),
            'history':[[ts,str(r)] for ts,r in pts], 'current_predicted_rate':str(rate),
            'forecast_rate_per_cycle':str(projected),'forecast_basis':'MIN_CURRENT_AND_7D_MEAN_CONSTANT_SCENARIO',
            'next_funding_ms':next_at,'interval_ms':interval,'forecast_cycles':cycles}


def evaluate(ex,coin,notional,hold_hours,margin_ratio,reserve_usdt,basis_stress_bps):
    out={'coin':coin,'status':'INVALID_DECLARED_GAP','reason':None,'expected_net_usdt':None,
         'capital_return':None,'rank':None,'mode':'READ_ONLY_ESTIMATE_NOT_EXECUTION_APPROVAL'}
    try:
        notional,hours,margin,reserve,stress=map(dec,(notional,hold_hours,margin_ratio,reserve_usdt,basis_stress_bps))
        if notional<=0 or hours<=0 or not 0<margin<=1 or reserve<0 or stress<0:
            raise ValueError('INVALID_SCENARIO')
        spot,perp=f'{coin}/USDT',f'{coin}/USDT:USDT'
        _,size=market(ex,perp);market(ex,spot)
        fees={}
        for symbol in (spot,perp):
            fee=ex.fetch_trading_fee(symbol)
            rate=dec(fee['taker'])
            if fee['symbol']!=symbol or not 0<=rate<1:raise ValueError('ACCOUNT_FEE_INVALID')
            fees[symbol]=rate
        now=int(time.time()*1000)
        prediction=forecast(ex,perp,now,hours)
        books={s:ex.fetch_order_book(s,limit=50) for s in (spot,perp)}
        view=copy.copy(ex);view.fetch_order_book=lambda symbol,limit=50:books[symbol]
        ask=dec(books[spot]['asks'][0][0])
        contracts=quantity(ex,perp,notional/ask/size)
        base=quantity(ex,spot,contracts*size)
        # 净值计算要求严格基础币中性，不能将未对冲方向风险计入套利收益。
        if base!=contracts*size:raise ValueError('QUANTITY_HEDGE_MISMATCH')
        quotes=[quote(view,spot,'buy',base),quote(view,perp,'sell',contracts),
                quote(view,spot,'sell',base),quote(view,perp,'buy',contracts)]
        if any(q['status']!='FILLED' for q in quotes):
            return out|{'status':'NOT_EXECUTABLE','reason':'INSUFFICIENT_DEPTH','quotes':quotes}
        check_now=int(time.time()*1000)
        if any(not 0<=check_now-q['book_timestamp']<=MAX_BOOK_AGE_MS for q in quotes):raise ValueError('SNAPSHOT_EXPIRED')
        if prediction['next_funding_ms']<=check_now:raise ValueError('FUNDING_BOUNDARY_CROSSED')
        sp_in,pe_in,sp_out,pe_out=map(lambda q:dec(q['notional_usdt']),quotes)
        fee_cost=sum((dec(q['notional_usdt'])*fees[q['symbol']] for q in quotes),dec(0))
        trading_cost=sp_in-sp_out+pe_out-pe_in
        total_cost=trading_cost+fee_cost
        mark=(dec(books[perp]['asks'][0][0])+dec(books[perp]['bids'][0][0]))/2
        funding=base*mark*dec(prediction['forecast_rate_per_cycle'])*prediction['forecast_cycles']
        net=funding-total_cost
        entry_fees=sp_in*fees[spot]+pe_in*fees[perp]
        capital=sp_in+pe_in*margin+reserve+entry_fees
        loss=base*ask*stress/10000
        per_cycle=base*mark*dec(prediction['forecast_rate_per_cycle'])
        breakeven=None
        if per_cycle>0:
            from decimal import ROUND_CEILING
            needed=max(1,int((total_cost/per_cycle).to_integral_value(rounding=ROUND_CEILING)))
            breakeven=str((dec(prediction['next_funding_ms']-now)+(needed-1)*prediction['interval_ms'])/DAY)
        return out|prediction|{'status':'CANDIDATE' if net>0 else 'REJECT_NET',
            'reason':None if net>0 else 'NONPOSITIVE_EXPECTED_NET','quotes':quotes,
            'fee_basis':'AUTHENTICATED_ACCOUNT_TAKER_USDT_EQUIVALENT_ESTIMATE',
            'fees':{s:str(v) for s,v in fees.items()},'base_quantity':str(base),'contracts':str(contracts),
            'hold_hours':str(hours),'entry_spot_usdt':str(sp_in),'entry_perp_usdt':str(pe_in),
            'fee_cost_usdt':str(fee_cost),'spread_slippage_cost_usdt':str(trading_cost),
            'roundtrip_cost_usdt':str(total_cost),'expected_funding_usdt':str(funding),
            'expected_net_usdt':str(net),'capital_usdt':str(capital),'capital_return':str(net/capital),
            'margin_ratio_assumption':str(margin),'reserve_usdt':str(reserve),
            'breakeven_days':breakeven,'basis_stress_bps':str(stress),'basis_stress_loss_usdt':str(loss),
            'stressed_net_usdt':str(net-loss),'valuation_basis':'CURRENT_BOOK_ROUNDTRIP_UNCHANGED_EXIT_SCENARIO',
            'computed_at_ms':check_now}
    except Exception as exc:
        return out|{'reason':str(exc) if isinstance(exc,ValueError) else type(exc).__name__}


def rank(ex,coins,**scenario):
    rows=[evaluate(ex,c,**scenario) for c in sorted(set(coins))]
    eligible=sorted((r for r in rows if r['status']=='CANDIDATE'),key=lambda r:(-dec(r['capital_return']),-dec(r['expected_net_usdt']),r['coin']))
    for n,r in enumerate(eligible,1):r['rank']=n
    return {'decision':'CANDIDATES_FOR_REVIEW' if eligible else 'HOLD_CASH',
            'ranking_basis':'EXPECTED_NET_OVER_TOTAL_CAPITAL','rows':sorted(rows,key=lambda r:(r['rank'] or 10**9,r['coin'])),
            'scope':'FORECAST_ONLY_NOT_ACCOUNT_RISK_OR_CAPITAL_ALLOCATION_APPROVAL'}


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('coins',nargs='+')
    for arg in ('notional','hold-hours','margin-ratio','reserve-usdt','basis-stress-bps'):
        parser.add_argument('--'+arg,required=True)
    args=parser.parse_args()
    names={'apiKey':'OKX_API_KEY','secret':'OKX_SECRET','password':'OKX_PASSWORD'}
    if any(not os.environ.get(n) for n in names.values()):
        print(json.dumps({'decision':'HOLD_CASH','status':'CONFIG_MISSING','expected_net_usdt':None}));return
    import ccxt
    from scan import PROXY
    ex=ccxt.okx({'timeout':15000,**{k:os.environ[n] for k,n in names.items()},**({'httpsProxy':PROXY} if PROXY else {})})
    ex.load_markets()
    scenario={k:v for k,v in vars(args).items() if k!='coins'}
    print(json.dumps(rank(ex,[c.upper() for c in args.coins],**scenario),ensure_ascii=False,indent=2))

if __name__=='__main__':main()
