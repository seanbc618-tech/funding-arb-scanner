from pathlib import Path
import hashlib,json,subprocess,time,os
root=Path('/home/sean/funding-arb');rel=root/'releases/unequal-72ec500'
v=json.loads((rel/'validation.json').read_text());assert v['passed']
for name,h in v['source_hashes'].items():assert hashlib.sha256((rel/name).read_bytes()).hexdigest()==h
smoke=json.loads(Path('/tmp/unequal-smoke-72ec500/report.json').read_text())
assert smoke['status']=='OBSERVING' and not smoke['coverage_gaps'] and not smoke['signal_gaps'] and len(smoke['rows'])==5
unit=Path.home()/'.config/systemd/user/funding-arb-unequal.service';assert not unit.exists(),'EXPERIMENT_SERVICE_ALREADY_EXISTS'
def system(*args):return subprocess.check_output(['systemctl','--user',*args],text=True)
original_pid=system('show','funding-arb-paper.service','-p','MainPID','--value').strip()
original_sources={p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.py')}
data=root/'experiments'/('unequal-v1-'+time.strftime('%Y%m%dT%H%M%SZ',time.gmtime()));data.mkdir(parents=True)
body=f'''[Unit]
Description=Independent unequal hedge PUBLIC PAPER experiment (five virtual groups)
After=network-online.target

[Service]
Type=simple
WorkingDirectory={rel}
Environment=PYTHONUNBUFFERED=1
UnsetEnvironment=OKX_API_KEY OKX_SECRET OKX_PASSWORD TELEGRAM_BOT_TOKEN
ExecStart={root}/.venv/bin/python -B {rel}/unequal_hedge.py --directory {data}
Restart=on-failure
RestartSec=15
UMask=0077
NoNewPrivileges=true
TimeoutStopSec=15

[Install]
WantedBy=default.target
'''
with unit.open('x') as f:f.write(body)
os.chmod(unit,0o600);system('daemon-reload');system('start','funding-arb-unequal.service')
assert system('show','funding-arb-paper.service','-p','MainPID','--value').strip()==original_pid
assert {p.name:hashlib.sha256(p.read_bytes()).hexdigest() for p in root.glob('*.py')}==original_sources
result=dict(release=str(rel),directory=str(data),main_paper_pid_unchanged=original_pid,main_sources_unchanged=True,unit_sha256=hashlib.sha256(body.encode()).hexdigest(),service=system('show','funding-arb-unequal.service','-p','MainPID','-p','ActiveState','-p','NRestarts'))
(rel/'activation.json').write_text(json.dumps(result,indent=2));print(json.dumps(result))
