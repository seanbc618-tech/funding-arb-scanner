"""冻结 V2 信号：完整 5m 数据聚合 15m，不补缺口、不使用未闭合柱。"""
from accounting import dec


def signals(rows,now,cfg):
    bars=sorted([r for r in rows if str(r[-1])=='1' and int(r[0])+300000<=now],key=lambda r:int(r[0]))
    if len(bars)<195 or not 0<=now-int(bars[-1][0])-300000<=150000:raise ValueError('V2_CANDLE_HISTORY_GAP')
    if any(int(b[0])-int(a[0])!=300000 for a,b in zip(bars,bars[1:])):raise ValueError('V2_CANDLE_INTERVAL_GAP')
    for b in bars:
        o,h,l,c=map(dec,b[1:5])
        if not 0<l<=min(o,c)<=max(o,c)<=h:raise ValueError('INVALID_CANDLE')
    buckets={}
    for b in bars:buckets.setdefault(int(b[0])//900000,[]).append(b)
    higher=[]
    for key,group in sorted(buckets.items()):
        if len(group)==3 and int(group[0][0])==key*900000 and (key+1)*900000<=now:
            higher.append((key*900000,dec(group[-1][4])))
    higher=higher[-65:]
    if len(higher)<64 or any(b[0]-a[0]!=900000 for a,b in zip(higher,higher[1:])):raise ValueError('V2_HIGHER_TIMEFRAME_GAP')
    cs=[b[1] for b in higher]
    def mean(v,n):return sum(v[-n:])/n
    def bear(v):return v[-1]<mean(v,20)<mean(v,60) and mean(v,20)<mean(v[:-3],20)
    regime=bear(cs);reset=not bear(cs) and not bear(cs[:-1])
    # 退出使用相反结构持续两根15m收盘；一次反弹不会立即抹掉趋势。
    def opposite(v):return v[-1]>mean(v,20) and mean(v,20)>mean(v[:-1],20)
    exit_signal=opposite(cs) and opposite(cs[:-1])
    close=[dec(b[4]) for b in bars]
    cross=close[-2]>=mean(close[:-1],20) and close[-1]<mean(close,20)
    ranges=[max(dec(b[2])-dec(b[3]),abs(dec(b[2])-dec(a[4])),abs(dec(b[3])-dec(a[4]))) for a,b in zip(bars,bars[1:])]
    return dict(at=int(bars[-1][0])+300000,short=regime and cross,exit=exit_signal,reset=reset,
                regime_at=higher[-1][0]+900000,regime=regime,entry_cross=cross,
                atr=str(sum(ranges[-14:])/14),support=str(min(dec(b[3]) for b in bars[-25:-1])),
                close=str(close[-1]),fast_15m=str(mean(cs,20)),slow_15m=str(mean(cs,60)))


def economics(signal,bid,ask,cfg):
    bid,ask=dec(bid),dec(ask)
    distance=max(dec(signal['atr'])*dec(cfg['atr_multiple']),ask*dec(cfg['min_stop_fraction']))
    cost=ask-bid+(ask+bid)*dec(cfg['perp_fee'])+bid*dec('0.001')
    room=bid-dec(signal['support'])
    # 历史支撑距离仅为筛选依据，不把它记作预测利润。
    return {'pass':room>=max(2*cost,dec('1.5')*distance), 'room':str(room),'cost':str(cost),'distance':str(distance)}


def rearm(meta,signal):
    if signal['regime_at']>meta.get('regime_seen',0):
        meta['regime_seen']=signal['regime_at']
        if signal['reset']:meta['armed']=True
