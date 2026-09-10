"""F16 预先冻结观察期，保存 paper 证据并离线验收；不代替实盘验收。"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import time
from unittest.mock import patch
from accounting import dec
import recovery_a


def encoded(value):return json.dumps(value,sort_keys=True,separators=(',',':'),ensure_ascii=False).encode()
def digest(value):return hashlib.sha256(encoded(value)).hexdigest()


def code_digest():
    root=Path(__file__).resolve().parent
    names=('paper_a.py','paper_funding.py','rank_a.py','allocate_a.py','check_a.py','exit_a.py','paper_loop.py','rotate_a.py','recovery_a.py','paper_acceptance.py','paper_replay.py','accounting.py','notify_events.py','scan.py')
    return digest({n:hashlib.sha256((root/n).read_bytes()).hexdigest() for n in names})


def immutable(path,value):
    with Path(path).open('xb') as f:
        os.chmod(path,0o600);f.write(encoded(value));f.flush();os.fsync(f.fileno())


def begin(directory,state,hours,max_gap_seconds,runtime_config):
    hours,gap=dec(hours),dec(max_gap_seconds)
    if hours<=0 or gap<=0:raise ValueError('POSITIVE_OBSERVATION_PERIOD_AND_GAP_REQUIRED')
    recovery_a.audit(state)
    directory=Path(directory);directory.mkdir(parents=True,exist_ok=False)
    immutable(directory/'baseline.json',state)
    contract={'version':1,'mode':'paper','started_at_ms':int(time.time()*1000),'hours':str(hours),
              'max_gap_seconds':str(gap),'runtime_config':runtime_config,'baseline_sha256':digest(state),'code_sha256':code_digest(),
              'required_events':['open','funding','rotation','exit','recovery','complete_cycle'],
              'scope':'PAPER_OBSERVATION_NOT_REAL_EXECUTION_OR_PROFITABILITY_PROOF'}
    immutable(directory/'contract.json',contract);return contract


def record(directory,state,report):
    """调用者持 paper 锁；每循环单独不可覆盖文件，中断遗留文件只会导致验收缺口。"""
    directory=Path(directory);contract=json.loads((directory/'contract.json').read_text())
    if contract['code_sha256']!=code_digest():raise ValueError('OBSERVATION_CODE_CHANGED')
    if report.get('runtime_config')!=contract['runtime_config']:raise ValueError('OBSERVATION_CONFIG_CHANGED')
    key=digest(state);snapshot=directory/f'state-{key}.json'
    if not snapshot.exists():immutable(snapshot,state)
    now=int(time.time()*1000)
    sample={'at_ms':now,'state_sha256':key,'report':report,'code_sha256':contract['code_sha256']}
    # 纳秒仅用于唯一文件名；判定连续性仍使用实际毫秒时间。
    immutable(directory/f'sample-{now}-{time.time_ns()}.json',sample)
    return key


def unfinished(state):
    return bool(state.get('pending') or any(t['phase'] not in ('open','closed') for t in state['trades'].values())
                or any(a['status']=='EXECUTING' for a in state.get('allocations',{}).values())
                or any(r['phase'] not in ('DONE','CASH') for r in state.get('rotations',[])))


def metrics(state):
    recovery_a.audit(state)
    flat=all(dec(p['quantity'])==0 for p in state['positions'].values())
    fees=sum((dec(o['quote']['fee_usdt']) for t in state['trades'].values() for o in t['orders']),dec(0))
    funding=sum((dec(e['payment_usdt']) for e in state.get('funding',{}).values() if e['status']=='POSTED'),dec(0))
    net=dec(state['cash_usdt'])-dec(state['initial_cash_usdt']) if flat and not unfinished(state) and not any(e['status']=='INVALID_DECLARED_GAP' for e in state.get('funding',{}).values()) else None
    return {'flat':flat,'fees_usdt':str(fees),'funding_usdt':str(funding),
            'closed_net_usdt':str(net) if net is not None else None,
            'price_pnl_usdt':str(net+fees-funding) if net is not None else None,
            'net_basis':'WHOLE_LEDGER_CLOSED_ONLY_OPEN_POSITION_NET_IS_NULL',
            'switch_attempts':len(state.get('rotations',[])),
            'switch_completed':sum(r['phase']=='DONE' for r in state.get('rotations',[]))}


def assess(directory,now_ms=None):
    directory=Path(directory);contract=json.loads((directory/'contract.json').read_text())
    baseline=json.loads((directory/'baseline.json').read_text())
    if digest(baseline)!=contract['baseline_sha256']:raise ValueError('BASELINE_HASH_MISMATCH')
    paths=sorted(directory.glob('sample-*.json'));gaps=[];states={};samples=[]
    previous=contract['started_at_ms'];limit=dec(contract['max_gap_seconds'])*1000
    for path in paths:
        sample=json.loads(path.read_text());stamp=sample['at_ms']
        if stamp<previous or stamp-previous>limit:gaps.append('OBSERVATION_TIME_GAP')
        previous=stamp
        if sample['code_sha256']!=contract['code_sha256']:gaps.append('CODE_CHANGED')
        key=sample['state_sha256']
        if key not in states:
            state=json.loads((directory/f'state-{key}.json').read_text())
            if digest(state)!=key:raise ValueError('STATE_HASH_MISMATCH')
            with patch('time.time',return_value=stamp/1000):recovery_a.audit(state)
            states[key]=state
        r=sample['report']
        if r.get('runtime_config')!=contract['runtime_config']:gaps.append('CONFIG_CHANGED')
        if not r.get('scanner_alive') or r.get('decision') in ('BLOCK','EXECUTION_REVIEW','EXIT_REVIEW','POSITION_OR_FUNDING_REVIEW','SCAN_UNAVAILABLE'):
            gaps.append('RUNTIME_REVIEW_OR_SCANNER_DOWN')
        for name in ('last_scan_success_ms','last_position_check_ms'):
            ts=r.get(name)
            warmup=name=='last_scan_success_ms' and stamp-contract['started_at_ms']<=limit and ts is None
            if not warmup and (ts is None or not 0<=stamp-ts<=limit):gaps.append('RUNTIME_FRESHNESS_GAP')
        samples.append(sample)
    now=int(time.time()*1000) if now_ms is None else now_ms
    if now<previous or now-previous>limit:gaps.append('OBSERVATION_TAIL_GAP')
    elapsed=(previous-contract['started_at_ms'])/3600000
    final=states[samples[-1]['state_sha256']] if samples else baseline
    if unfinished(final):gaps.append('UNFINISHED_FINAL_EXECUTION')
    if any(e['status']=='INVALID_DECLARED_GAP' for e in final.get('funding',{}).values()):gaps.append('FUNDING_DATA_GAP')
    expected=set()
    for sample in samples:
        snapshot=states[sample['state_sha256']]
        for ident,check in sample['report'].get('positions',{}).items():
            at=check.get('metrics',{}).get('funding_timestamp')
            if at is not None and ident in snapshot['trades']:
                expected.add((f"{snapshot['trades'][ident]['coin']}/USDT:USDT",int(at)))
    import paper_funding
    for symbol,at in expected:
        if at>previous-60000:continue
        try:
            if paper_funding.held_base(final,symbol,at,previous)>0 and final.get('funding',{}).get(f'{symbol}:{at}',{}).get('status')!='POSTED':gaps.append('EXPECTED_SETTLEMENT_MISSING')
        except ValueError:gaps.append('SETTLEMENT_POSITION_EVIDENCE_GAP')
    start=contract['started_at_ms'];events={k:False for k in contract['required_events']}
    # 事件必须发生在本观察期；旧版本历史不能替新版本完成验收。
    period_trades=[t for t in final['trades'].values() if t['opened_at']*1000>=start and t['trade_id'] not in baseline['trades']]
    events['open']=any(any(dec(o['quote']['filled'])>0 for o in t['orders']) for t in period_trades)
    period_ids={t['trade_id'] for t in period_trades}
    events['exit']=any(t['phase']=='closed' and any(dec(o['quote']['filled'])>0 for o in t['orders']) for t in period_trades)
    events['funding']=any(e['status']=='POSTED' and dec(e.get('base_short','0'))>0 and e['funding_time_ms']>=start for e in final.get('funding',{}).values())
    events['rotation']=any(r['phase']=='DONE' and r['started_at']*1000>=start and r['old_trade_id'] in period_ids for r in final.get('rotations',[]))
    events['recovery']=any(r['at_ms']>=start and any(a['action'] in ('EXIT_KNOWN_REMAINDER','CONFIRMED_FLAT','ABANDONED_UNAPPLIED_PAPER_INTENT') for a in r['actions']) for r in final.get('recoveries',[]))
    for rotation in final.get('rotations',[]):
        if rotation['phase']!='DONE' or rotation['old_trade_id'] not in period_ids:continue
        matches=[t for t in period_trades if t.get('allocation_id')==rotation.get('allocation_id')]
        old=final['trades'][rotation['old_trade_id']]
        if len(matches)!=1 or old['phase']!='closed' or matches[0]['phase']!='closed':continue
        chain=(old,matches[0])
        if any(e['status']=='POSTED' and dec(e.get('base_short','0'))>0 and any(e['symbol']==f"{t['coin']}/USDT:USDT" and t['opened_at']*1000<e['funding_time_ms']<t['closed_at']*1000 for t in chain) for e in final.get('funding',{}).values()):
            events['complete_cycle']=True
    missing=[k for k,v in events.items() if not v]
    status='INVALID_DECLARED_GAP' if gaps else 'OBSERVING' if elapsed<float(contract['hours']) else 'INCOMPLETE_EVENTS' if missing else 'PASS_PAPER_OBSERVATION'
    financial=metrics(final)
    if gaps:financial.update(closed_net_usdt=None,price_pnl_usdt=None)
    return {'status':status,'elapsed_hours':elapsed,'required_hours':contract['hours'],'samples':len(samples),
            'events':events,'missing_events':missing,'gaps':sorted(set(gaps)), 'metrics':financial,
            'switch_attempts_per_day':len([r for r in final.get('rotations',[]) if r['started_at']*1000>=start])/(elapsed/24) if elapsed>0 else None,
            'scope':contract['scope']}


def main():
    parser=argparse.ArgumentParser(description=__doc__);sub=parser.add_subparsers(dest='command',required=True)
    b=sub.add_parser('begin');b.add_argument('directory');b.add_argument('--state',required=True);b.add_argument('--config',required=True);b.add_argument('--hours',required=True);b.add_argument('--max-gap-seconds',required=True)
    a=sub.add_parser('assess');a.add_argument('directory')
    args=parser.parse_args()
    result=begin(args.directory,json.loads(Path(args.state).read_text()),args.hours,args.max_gap_seconds,json.loads(Path(args.config).read_text())) if args.command=='begin' else assess(args.directory)
    print(json.dumps(result,ensure_ascii=False,indent=2))
if __name__=='__main__':main()
