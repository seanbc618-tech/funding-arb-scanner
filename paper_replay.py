"""F16 冻结数据逐帧回放；缺盘口/实际结算证据即声明缺口，不补造历史。"""
import argparse
import copy
import json
from pathlib import Path
import queue
from unittest.mock import patch
import ccxt
import paper_a
import paper_loop
import paper_acceptance as acceptance


class ReplayExchange:
    def __init__(self,dataset,frame):
        self.markets=dataset['markets'];self.frame=frame
        self.precision=ccxt.okx();self.precision.markets=self.markets
        self.precision.precisionMode=dataset['precision_mode']
    def amount_to_precision(self,s,q):return self.precision.amount_to_precision(s,q)
    def price_to_precision(self,s,q):return self.precision.price_to_precision(s,q)
    def fetch_order_book(self,s,limit=50):return copy.deepcopy(self.frame['books'][s])
    def fetch_trading_fee(self,s):return copy.deepcopy(self.frame['fees'][s])
    def fetch_funding_rate(self,s):return copy.deepcopy(self.frame['funding'][s])
    def publicGetPublicFundingRateHistory(self,params):
        rows=self.frame['history'][params['instId']]
        if 'after' in params:rows=[r for r in rows if int(r['fundingTime'])<int(params['after'])]
        if 'before' in params:rows=[r for r in rows if int(r['fundingTime'])>int(params['before'])]
        return {'code':'0','data':sorted(rows,key=lambda r:-int(r['fundingTime']))[:int(params.get('limit',100))]}
    def publicGetMarketHistoryMarkPriceCandles(self,params):
        rows=self.frame['mark_candles'].get(params['instId'],[])
        return {'code':'0','data':[r for r in rows if int(params['before'])<int(r[0])<int(params['after'])]}


def validate_frame(dataset,frame):
    now=frame['at_ms']
    for coin in dataset['coins']:
        for symbol in (f'{coin}/USDT',f'{coin}/USDT:USDT'):
            if symbol not in frame['books'] or symbol not in frame['fees']:raise ValueError('INVALID_DECLARED_GAP_UNIVERSE_DATA')
            if not 0<=now-frame['books'][symbol]['timestamp']<=5000:raise ValueError('INVALID_DECLARED_GAP_BOOK_TIME')
        perp=f'{coin}/USDT:USDT';inst=dataset['markets'][perp]['id']
        if perp not in frame['funding'] or inst not in frame['history']:raise ValueError('INVALID_DECLARED_GAP_FUNDING')
        if any(int(r['fundingTime'])>now for r in frame['history'][inst]):raise ValueError('CAUSALITY_VIOLATION_FUTURE_HISTORY')
        if any(int(r[0])+60000>now for r in frame.get('mark_candles',{}).get(inst,[])):raise ValueError('CAUSALITY_VIOLATION_UNFINISHED_CANDLE')


def replay(dataset,directory):
    if dataset.get('source') not in ('RECORDED_PUBLIC_DATA','SYNTHETIC_SCENARIO'):raise ValueError('EXPLICIT_DATA_SOURCE_REQUIRED')
    if dataset['ccxt_version']!=ccxt.__version__:raise ValueError('PRECISION_LIBRARY_VERSION_MISMATCH')
    if not dataset['frames'] or len(dataset['coins'])!=len(set(dataset['coins'])):raise ValueError('INVALID_DATASET')
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    acceptance.immutable(directory/'dataset.json',dataset)
    state=paper_a.initial(dataset['cash_usdt'],dataset['spot_fee'],dataset['perp_fee']);path=directory/'paper.json';paper_a.save(path,state)
    reports=[];gaps=[];previous=-1;expected=set();basis=[]
    for index,frame in enumerate(dataset['frames']):
        now=frame['at_ms']
        if now<=previous:raise ValueError('NONMONOTONIC_REPLAY_TIME')
        previous=now
        try:validate_frame(dataset,frame)
        except (ValueError,KeyError,TypeError) as exc:
            gaps.append({'frame':index,'gap':str(exc)});continue
        ex=ReplayExchange(dataset,frame)
        with patch('time.time',return_value=now/1000):
            state=json.loads(path.read_text())
            ranking,holdings,switch_rankings=paper_loop.scan(ex,state,dataset['coins'],dataset['scenario'],True)
            funding=paper_loop.collect_funding(ex,state)
            snapshot={'started_at_ms':now,'completed_at_ms':now,'state_identity':paper_loop.identity(state),'gap':None,
                      'ranking':ranking,'holdings':holdings,'switch_rankings':switch_rankings,'funding':funding}
            inbox=queue.Queue();inbox.put(snapshot)
            report=paper_loop.tick(path,ex,inbox,dataset['limits'],auto_exit=True,auto_recover=True,switch_policy=dataset['switch_policy'])
            reports.append({'frame':index,'at_ms':now,'report':report})
            for row in ranking['rows']:
                if row['status']=='INVALID_DECLARED_GAP':gaps.append({'frame':index,'coin':row['coin'],'gap':row['reason']})
            if report.get('decision') in ('BLOCK','POSITION_OR_FUNDING_REVIEW','SCAN_UNAVAILABLE'):gaps.append({'frame':index,'gap':report['decision']})
            for coin in dataset['coins']:
                sp,pe=f'{coin}/USDT',f'{coin}/USDT:USDT'
                b1,b2=frame['books'][sp],frame['books'][pe]
                mid1=sum(paper_a.dec(b1[s][0][0]) for s in ('asks','bids'))/2
                mid2=sum(paper_a.dec(b2[s][0][0]) for s in ('asks','bids'))/2
                basis.append({'at_ms':now,'coin':coin,'basis_bps':str((mid2/mid1-1)*10000)})
                expected.add((pe,int(frame['funding'][pe]['fundingTimestamp'])))
    state=json.loads(path.read_text())
    with patch('time.time',return_value=previous/1000):
        for symbol,at in sorted(expected):
            if at>previous-60000:continue
            import paper_funding
            try:
                base=paper_funding.held_base(state,symbol,at,previous)
                if base and state.get('funding',{}).get(f'{symbol}:{at}',{}).get('status')!='POSTED':gaps.append({'gap':'INVALID_DECLARED_GAP_EXPECTED_SETTLEMENT','symbol':symbol,'at_ms':at})
            except ValueError as exc:gaps.append({'gap':str(exc),'symbol':symbol,'at_ms':at})
        financial=acceptance.metrics(state)
    if gaps:financial.update(closed_net_usdt=None,price_pnl_usdt=None)
    result={'status':'INVALID_DECLARED_GAP' if gaps else 'PASS_SYNTHETIC_REPLAY' if dataset['source']=='SYNTHETIC_SCENARIO' else 'PASS_RECORDED_REPLAY',
            'source':dataset['source'],'dataset_sha256':acceptance.digest(dataset),'frames':len(dataset['frames']),
            'gaps':gaps,'metrics':financial,'basis':basis,'reports':reports,
            'scope':'REPLAY_NOT_CONTINUOUS_OBSERVATION_OR_REAL_PROFITABILITY_PROOF'}
    acceptance.immutable(directory/'result.json',result);return result


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('dataset');parser.add_argument('--output',required=True)
    args=parser.parse_args();result=replay(json.loads(Path(args.dataset).read_text()),args.output)
    print(json.dumps({k:v for k,v in result.items() if k not in ('reports','basis')},ensure_ascii=False,indent=2))
if __name__=='__main__':main()
