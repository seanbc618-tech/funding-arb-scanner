"""只读 paper 账本的 Telegram 成交事件与每小时账户摘要；不调用交易接口。"""
import argparse
import fcntl
import json
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo
from accounting import dec
import paper_a
import recovery_a
from notify_events import save
from notify_run import send_message

TZ = ZoneInfo('Asia/Shanghai')


def money(value):
    return '未知' if value is None else f'{dec(value):,.4f}'


def valuation(state, report, now):
    recovery_a.audit(state)
    entry = sum((dec(p['entry_notional_usdt']) for p in state['positions'].values() if p['kind']=='spot'), dec(0))
    realized = dec(state['cash_usdt'])+entry-dec(state['initial_cash_usdt'])
    floating=dec(0);exit_fees=dec(0);gaps=[]
    quotes={}
    for check in report.get('positions',{}).values():
        for name in ('spot_execution','perp_execution'):
            q=check.get('metrics',{}).get(name)
            if q:quotes[q['symbol']]=q
    if not 0<=now*1000-report.get('completed_at_ms',0)<=5000:gaps.append('模拟行情快照过期')
    for symbol,p in state['positions'].items():
        qty=dec(p['quantity'])
        if not qty:continue
        q=quotes.get(symbol)
        if (not q or abs(dec(q['amount'])-qty)>dec('1e-12')
                or q['side']!=('sell' if p['kind']=='spot' else 'buy')
                or not 0<=now*1000-q['book_timestamp']<=5000):
            gaps.append(symbol+'缺少对应数量的新鲜退出报价');continue
        value=qty*dec(p['contract_size'])*dec(q['vwap'])
        floating += value-dec(p['entry_notional_usdt']) if p['kind']=='spot' else dec(p['entry_notional_usdt'])-value
        exit_fees += value*dec(state['fees'][p['kind']])
    if state.get('pending'):gaps.append('存在待确认模拟执行')
    if any(e['status']=='INVALID_DECLARED_GAP' for e in state.get('funding',{}).values()):gaps.append('资金费数据不完整')
    pnl=realized+floating if not gaps else None
    return dict(realized=realized,floating=floating if not gaps else None,pnl=pnl,
                equity=dec(state['initial_cash_usdt'])+pnl if pnl is not None else None,
                exit_net=pnl-exit_fees if pnl is not None else None,gaps=gaps)


def summary(state,report,now):
    v=valuation(state,report,now)
    fees=sum((dec(o['quote']['fee_usdt']) for t in state['trades'].values() for o in t['orders']),dec(0))
    funding=sum((dec(e['payment_usdt']) for e in state.get('funding',{}).values() if e['status']=='POSTED'),dec(0))
    lines=['【资金费率｜PAPER模拟账户】',datetime.fromtimestamp(now,TZ).strftime('%m-%d %H:%M:%S 北京时间'),
           f"初始本金：{money(state['initial_cash_usdt'])} USDT",
           f"模拟净值：{money(v['equity'])} USDT",
           f"累计净盈亏：{money(v['pnl'])} USDT",
           f"已实现账面损益：{money(v['realized'])} USDT（含已付手续费、已入账资金费）",
           f"持仓浮动盈亏：{money(v['floating'])} USDT",
           f"估算立即平仓后累计净盈亏：{money(v['exit_net'])} USDT（再扣平仓手续费）",
           f"累计资金费：{money(funding)}；累计手续费：{money(fees)} USDT",
           f"现金：{money(state['cash_usdt'])}；可用资金：{money(paper_a.available(state))} USDT"]
    active=[(s,p) for s,p in state['positions'].items() if dec(p['quantity'])]
    for symbol,p in active:
        lines.append(f"持仓 {symbol}：{'现货' if p['kind']=='spot' else '永续空头'} {p['quantity']}{'币' if p['kind']=='spot' else '张'}")
    if not active:lines.append('当前空仓')
    if v['gaps']:lines.append('估值缺口：'+'；'.join(v['gaps']))
    lines.append('报价按当前退出侧深度估值，非成交保证；仅模拟，不是真实OKX账户。')
    return '\n'.join(lines)


def closed_net(state, trade):
    """已平双腿成交价差、费用与按历史持仓归属的已入账资金费。"""
    net=dec(0)
    for order in trade['orders']:
        q=order['quote']
        net+=dec(q['notional_usdt'])*(1 if q['side']=='sell' else -1)-dec(q['fee_usdt'])
    for e in state.get('funding',{}).values():
        if e['symbol']!=trade['coin']+'/USDT:USDT':continue
        held=dec(0)
        for o in trade['orders']:
            q=o['quote']
            if q['kind']=='perp' and o['executed_at_ms']<e['funding_time_ms']:
                held+=dec(q['filled'])*dec(q['contract_size'])*(1 if q['side']=='sell' else -1)
        if not held:continue
        if e['status']!='POSTED':return None
        net+=held*dec(e['mark_price'])*dec(e['realized_rate'])
    return net


def events(state):
    result={}
    for ident,t in state['trades'].items():
        for o in t['orders']:
            q=o['quote'];opening=(q['kind']=='spot' and q['side']=='buy') or (q['kind']=='perp' and q['side']=='sell')
            result['order:'+o['order_id']]=(o['executed_at_ms'], '\n'.join([
                '【资金费率｜PAPER模拟'+('开仓成交' if opening else '平仓成交')+'】',
                f"{t['coin']}｜{'现货' if q['kind']=='spot' else '永续'}｜{q['side']}",
                f"时间：{datetime.fromtimestamp(o['executed_at_ms']/1000,TZ).strftime('%m-%d %H:%M:%S')}",
                f"成交：{q['filled']} / 请求 {q['requested']}；均价：{q['vwap']}",
                f"金额：{money(q['notional_usdt'])}；手续费：{money(q['fee_usdt'])} USDT",
                f"结果：{q['status']}；交易编号：{ident[:10]}", '单腿记录，双腿完成情况以账户摘要为准。']))
        if t['phase']=='closed':
            filled=any(dec(o['quote']['filled']) for o in t['orders'])
            if filled:result['closed:'+ident]=(int(t['closed_at']*1000),f"【资金费率｜PAPER模拟平仓完成】\n{t['coin']} 双腿已平\n本笔已记录净损益：{money(closed_net(state,t))} USDT（含手续费及已入账资金费）\n未入账资金费可能使结果后续调整。\n交易编号：{ident[:10]}")
    for key,e in state.get('funding',{}).items():
        if e['status']=='NO_POSITION':continue
        result['funding:'+key+':'+e['status']]=(e.get('checked_at_ms',e['funding_time_ms']),
            f"【资金费率｜PAPER模拟资金费】\n{e['symbol']}\n状态：{e['status']}\n本次入账：{money(e.get('payment_usdt'))} USDT\n结算时间：{datetime.fromtimestamp(e['funding_time_ms']/1000,TZ).isoformat()}")
    return result


def tick(root, sender=send_message, now=None):
    now=time.time() if now is None else now
    # 账本与报告均由原子替换发布；只读单个完整快照，不争抢交易循环锁。
    # 估值另外校验报价时间、方向及数量，错配时净值留空。
    state=json.loads((root/'paper_a_state.json').read_text())
    report=json.loads((root/'paper_loop_status.json').read_text())
    recovery_a.audit(state)
    path=root/'paper_notify_state.json';all_events=events(state)
    existing=path.exists()
    saved=json.loads(path.read_text()) if existing else {'version':1,'events':{k:{'status':'BASELINE'} for k in all_events},'hour':None}
    if saved.get('version')!=1:raise ValueError('INVALID_NOTIFICATION_STATE')
    # 第一次只发送当前账户，不补发整本历史。意图先落盘，不重试未知投递。
    if not existing:save(path,saved)
    deliveries=[]
    def deliver(key,text):
        saved['events'][key]={'status':'ATTEMPTING','at':now};save(path,saved)
        receipt=sender(text);saved['events'][key].update(receipt);save(path,saved);deliveries.append(receipt)
    fresh=sorted(((key,*event) for key,event in all_events.items() if key not in saved['events']),key=lambda v:(v[1],v[0]))
    for key,_,text in fresh:deliver(key,text)
    hour=datetime.fromtimestamp(now,TZ).strftime('%Y-%m-%d/%H')
    if fresh or saved['hour']!=hour:
        key='summary:'+str(time.time_ns())
        saved['hour']=hour
        deliver(key,summary(state,report,now))
    return {'status':'OK','deliveries':deliveries,'new_events':len(fresh)}


def main():
    parser=argparse.ArgumentParser(description=__doc__);parser.add_argument('--root',type=Path,default=Path(__file__).resolve().parent);parser.add_argument('--preview',action='store_true');args=parser.parse_args()
    if args.preview:
        print(summary(json.loads((args.root/'paper_a_state.json').read_text()),json.loads((args.root/'paper_loop_status.json').read_text()),time.time()));return
    with (args.root/'paper_notify.lock').open('a') as lock:
        fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB)
        while True:
            try:print(json.dumps(tick(args.root)),flush=True)
            except BlockingIOError:pass
            time.sleep(10)

if __name__=='__main__':main()
