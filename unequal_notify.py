"""独立不等量实验统一播报；只读实验文件，独立游标，无交易 API。"""
import argparse
from collections import Counter
from datetime import datetime
import fcntl
import json
from pathlib import Path
import time
from zoneinfo import ZoneInfo
from accounting import dec
from notify_events import save
from notify_run import send_message

TZ=ZoneInfo('Asia/Shanghai')
LABELS={'OVERLAY_OPEN':'额外空头开仓','STOP':'止损买回','TREND_EXIT':'趋势退出','TIME_EXIT':'持有超时退出',
        'DATA_GAP_EXIT':'数据异常退出','RISK_OR_END':'风控或到期退出','EXPERIMENT_END':'到期平仓','COMMON_BASELINE':'等量底仓'}

def money(x):return '未知' if x is None else f'{dec(x):.2f}'
def load(directory):
    return json.loads((directory/'state.json').read_text()),json.loads((directory/'report.json').read_text())
def events(state):
    out={}
    for ratio,a in state['groups'].items():
        for i,e in enumerate(a['events']):
            if e['type']=='FILL' and dec(e['order']['quote']['filled'])>0:
                q=e['order']['quote'];key=f'{ratio}:{i}'
                out[key]=f"{ratio}倍 {e['order']['trade_id']} {LABELS.get(e['reason'],e['reason'])} {q['filled']}{'张' if q['kind']=='perp' else '币'} @ {q['vwap']}"
            elif e['type']=='FUNDING':out[f'{ratio}:{i}']=f"{ratio}倍 资金费 {money(e['payment_usdt'])} USDT"
    return out

def summary(state,report,now,new):
    age=(now*1000-report.get('at_ms',0))/1000
    version=state.get('config',{}).get('display_version',state.get('config',{}).get('strategy','v1').upper())
    lines=[f"【不等量对冲 {version}｜独立PAPER五组】",datetime.fromtimestamp(now,TZ).strftime('%m-%d %H:%M:%S 北京时间'),
           f"已观察 {(now*1000-state['started_at_ms'])/3600000:.1f} 小时｜状态 {report.get('status','UNKNOWN')}",
           '每组本金 20,000 USDT；倍数为额外信号触发后的目标上限。']
    if new:
        lines.append(f'新增成交/结算记录 {len(new)} 条：')
        lines.extend(new[:16])
        if len(new)>16:lines.append(f'另有 {len(new)-16} 条，完整记录保存在实验账本。')
    if not 0<=age<=5:
        lines.append('当前估值未知：报告超过 5 秒或时钟异常。')
    else:
        lines.append('倍数｜账面净值｜估算全平净盈亏｜累计手续费')
        for r in report.get('rows',[]):
            lines.append(f"{r['ratio']}×｜{money(r['equity'])}｜{money(r['diagnostic_net_pnl'])}｜{money(r['fees'])}")
        lines.append('金额单位 USDT；全平损益含预计退出手续费。')
    rows=report.get('rows',[])
    for r in rows:
        lines.append(f"{r['ratio']}×：额外开仓 {r['overlay_entries']} 次，止损成交 {r['stop_fills']} 次；当前 BTC/ETH 比例 "+'/'.join(money(r['realized_ratios'].get(c)) for c in ('BTC','ETH')))
    gaps=report.get('coverage_gaps',state.get('coverage_gaps',[]))
    funding_gap=any(r.get('funding_gaps') for r in rows)
    if gaps or funding_gap:
        lines.append(f'⚠ 历史行情缺口 {len(gaps)} 次'+('，结算证据未齐' if funding_gap else '')+'。上述盈亏仅为已记录路径诊断，正式比较无效，不能据此评选最佳倍数。')
    if report.get('signal_gaps'):lines.append('当前入场信号数据不完整。')
    if not rows:lines.append('当前报告无估值，待恢复；不沿用旧价格。')
    lines.append('原资金套利账户另行播报；此处仅模拟，无真实订单。')
    return '\n'.join(lines)

def tick(directory,cursor,sender=send_message,now=None):
    state,report=load(directory);now=time.time() if now is None else now;all_events=events(state)
    previous=json.loads(cursor.read_text()) if cursor.exists() else None
    if previous and previous['started_at_ms']!=state['started_at_ms']:raise ValueError('EXPERIMENT_CURSOR_MISMATCH')
    hour=datetime.fromtimestamp(now,TZ).strftime('%Y-%m-%d/%H')
    fresh=[v for k,v in all_events.items() if previous and k not in previous['seen']]
    health=[report.get('status'),bool(report.get('coverage_gaps',state.get('coverage_gaps'))),bool(report.get('signal_gaps')),
            bool(any(r.get('funding_gaps') for r in report.get('rows',[]))),not 0<=now*1000-report.get('at_ms',0)<=15000]
    if previous and not fresh and previous['hour']==hour and previous['health']==health:return {'status':'IDLE'}
    text=summary(state,report,now,fresh)
    saved=dict(version=1,started_at_ms=state['started_at_ms'],hour=hour,health=health,seen=list(all_events),status='ATTEMPTING',attempted_at=now)
    # 发送前落盘，不因重启或未知回执重发历史。初次发送汇总，不补发旧成交。
    save(cursor,saved)
    try:receipt=sender(text)
    except Exception:receipt={'status':'DELIVERY_UNKNOWN'}
    saved.update(receipt);save(cursor,saved)
    return dict(status=saved['status'],message_id=saved.get('message_id'),new_events=len(fresh))

def main():
    p=argparse.ArgumentParser(description=__doc__);p.add_argument('--directory',type=Path,required=True);p.add_argument('--cursor',type=Path);p.add_argument('--preview',action='store_true');p.add_argument('--once',action='store_true');a=p.parse_args()
    if a.preview:
        s,r=load(a.directory);print(summary(s,r,time.time(),[]));return
    if a.cursor is None:p.error('--cursor required')
    a.cursor.parent.mkdir(parents=True,exist_ok=True)
    with a.cursor.with_suffix('.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            print(json.dumps(tick(a.directory,a.cursor)),flush=True)
            if a.once:return
            time.sleep(10)
if __name__=='__main__':main()
