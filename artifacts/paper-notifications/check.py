import sys,time,copy,tempfile,json
from pathlib import Path
from unittest.mock import patch
sys.path.insert(0,str(Path(__file__).resolve().parents[2]));sys.path.insert(0,str(Path(__file__).resolve().parents[2]/'checks'))
import paper_a as p,paper_notify as n
from f08_paper import Exchange
with tempfile.TemporaryDirectory() as tmp:
 root=Path(tmp);path=root/'paper_a_state.json';s=p.initial(1000,'.001','.001');p.save(path,s);ex=Exchange()
 t=p.operate(s,path,ex,'BTC',100);now=time.time();qs={}
 for sym,pos in s['positions'].items():
  qs['spot_execution' if pos['kind']=='spot' else 'perp_execution']={'symbol':sym,'side':'sell' if pos['kind']=='spot' else 'buy','amount':pos['quantity'],'book_timestamp':now*1000,'vwap':'100'}
 r={'completed_at_ms':now*1000,'positions':{t['trade_id']:{'metrics':qs}}};(root/'paper_loop_status.json').write_text(json.dumps(r))
 v=n.valuation(s,r,now);assert abs(v['pnl']-n.dec('-.2999'))<n.dec('1e-12'),v
 assert abs(v['exit_net']-n.dec('-.4999'))<n.dec('1e-12'),v
 assert n.valuation(s,r,now+6)['pnl'] is None
 bad=copy.deepcopy(r);bad['positions'][t['trade_id']]['metrics']['spot_execution']['amount']='2';assert n.valuation(s,bad,now)['pnl'] is None
 messages=[]
 def sender(text):messages.append(text);return {'status':'CONFIRMED','message_id':len(messages)}
 n.tick(root,sender,now);assert len(messages)==1 and 'PAPER模拟账户' in messages[0]
 n.tick(root,sender,now);assert len(messages)==1
 p.operate(s,path,ex,'BTC',closing=True)
 assert n.closed_net(s,s['trades'][t['trade_id']])==n.dec(s['cash_usdt'])-1000
 n.tick(root,sender,now);assert len(messages)==5,len(messages)
 assert any('平仓完成' in m for m in messages);n.tick(root,sender,now);assert len(messages)==5
 # 不确定投递不重复，重启也不重发。
 later=now+3600
 def unknown(text):messages.append(text);return {'status':'DELIVERY_UNKNOWN'}
 n.tick(root,unknown,later);count=len(messages);n.tick(root,unknown,later);assert len(messages)==count
 print(json.dumps({'status':'PASS','cases':['equity_with_open_spot_and_perp','exit_fee_deduction','stale_quote_null','wrong_size_null','baseline_no_history_spam','no_duplicate','close_net_matches_cash','both_leg_events_and_close','unknown_delivery_no_retry']}))
