from pathlib import Path
import json,subprocess,time,hashlib
from decimal import Decimal
root=Path('/home/sean/funding-arb');rel=root/'releases/unequal-v21-daa0522'
old=root/'experiments/unequal-v2-20260915T092647Z';units=Path.home()/'.config/systemd/user'
strategy='funding-arb-unequal-v2.service';notifier='funding-arb-unequal-v2-notify.service'
def system(*args):return subprocess.check_output(['systemctl','--user',*args],text=True).strip()
smoke=json.loads(Path('/tmp/unequal-v21-smoke-daa0522/report.json').read_text())
assert not smoke['coverage_gaps'] and not smoke['signal_gaps'] and smoke['rows']
original={n:(units/n).read_text() for n in (strategy,notifier)}
preserved={n:system('show',n,'-p','MainPID','--value') for n in ['funding-arb-paper.service','funding-arb-unequal.service','funding-arb-unequal-notify.service']}
system('stop',notifier,strategy)
try:
 s=json.loads((old/'state.json').read_text())
 assert all(Decimal(m['extra'])==0 for a in s['groups'].values() for m in a['meta'].values()),'OLD_OVERLAY_NOT_FLAT'
 assert all(not a['pending_actions'] for a in s['groups'].values()),'OLD_ACTION_PENDING'
 data=root/'experiments'/('unequal-v21-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()))
 data.mkdir()
 for n,body in original.items():
  (rel/(n+'.previous')).write_text(body)
  body=body.replace(str(root/'releases/unequal-v2-f21afaf'),str(rel)).replace(str(old),str(data))
  body=body.replace('--v2 --directory','--v21 --directory').replace('unequal-v2.json','unequal-v21.json').replace('V2 ', 'V2.1 ')
  (units/n).write_text(body)
 system('daemon-reload');system('start',strategy)
 for _ in range(20):
  time.sleep(2)
  if (data/'report.json').exists():
   report=json.loads((data/'report.json').read_text())
   if report.get('rows') and not report['coverage_gaps'] and not report['signal_gaps']:break
 else:raise RuntimeError('NEW_STARTUP_NOT_HEALTHY')
 system('start',notifier)
 assert {n:system('show',n,'-p','MainPID','--value') for n in preserved}==preserved
 result={'release':str(rel),'directory':str(data),'previous_directory':str(old),'previous_status':'SUPERSEDED_EARLY_INVALID_GAPS','previous_state_sha256':hashlib.sha256((old/'state.json').read_bytes()).hexdigest(),'previous_final_report':json.loads((old/'report.json').read_text()),'preserved_pids':preserved,'activated_at_ms':int(time.time()*1000)}
 (rel/'activation.json').write_text(json.dumps(result,indent=2));print(json.dumps({k:v for k,v in result.items() if k!='previous_final_report'}))
except Exception:
 system('stop',strategy,notifier)
 for n,body in original.items():(units/n).write_text(body)
 system('daemon-reload');system('start',strategy,notifier)
 raise
