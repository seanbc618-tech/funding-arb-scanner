from pathlib import Path
import json,hashlib,subprocess,time,os
root=Path('/home/sean/funding-arb');rel=root/'releases/unequal-v2-f21afaf';units=Path.home()/'.config/systemd/user'
v=json.loads((rel/'validation.json').read_text());assert v['passed']
for n,h in v['source_hashes'].items():assert hashlib.sha256((rel/n).read_bytes()).hexdigest()==h
smoke=json.loads(Path('/tmp/unequal-v2-smoke-f21afaf/report.json').read_text());assert smoke['strategy']=='v2' and not smoke['coverage_gaps'] and not smoke['signal_gaps']
def system(*args):return subprocess.check_output(['systemctl','--user',*args],text=True)
old={s:system('show',s,'-p','MainPID','--value').strip() for s in ['funding-arb-paper.service','funding-arb-unequal.service','funding-arb-unequal-notify.service']}
original={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.py')}
data=root/'experiments'/('unequal-v2-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()));data.mkdir(parents=True)
unit=units/'funding-arb-unequal-v2.service';notify=units/'funding-arb-unequal-v2-notify.service';assert not unit.exists() and not notify.exists()
body=f'''[Unit]
Description=Unequal hedge V2 PUBLIC PAPER five groups
After=network-online.target
[Service]
Type=simple
WorkingDirectory={rel}
Environment=PYTHONUNBUFFERED=1
UnsetEnvironment=OKX_API_KEY OKX_SECRET OKX_PASSWORD TELEGRAM_BOT_TOKEN
ExecStart={root}/.venv/bin/python -B {rel}/unequal_hedge.py --v2 --directory {data}
Restart=on-failure
RestartSec=15
UMask=0077
NoNewPrivileges=true
'''
nb=f'''[Unit]
Description=Unequal hedge V2 unified PAPER notifications
After=network-online.target
[Service]
Type=simple
WorkingDirectory={rel}
EnvironmentFile=%h/.config/funding-arb/notify.env
Environment=PYTHONUNBUFFERED=1
ExecStart={root}/.venv/bin/python -B {rel}/unequal_notify.py --directory {data} --cursor {root}/notification-state/unequal-v2.json
Restart=on-failure
RestartSec=15
UMask=0077
NoNewPrivileges=true
'''
for p,b in [(unit,body),(notify,nb)]:
 with p.open('x') as f:f.write(b)
 os.chmod(p,0o600)
system('daemon-reload');system('start','funding-arb-unequal-v2.service')
assert {s:system('show',s,'-p','MainPID','--value').strip() for s in old}==old
assert {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.py')}==original
result=dict(release=str(rel),directory=str(data),previous_pids_unchanged=old,root_sources_unchanged=True)
(rel/'activation.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
