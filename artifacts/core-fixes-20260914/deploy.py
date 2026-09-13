from pathlib import Path
import sys,json,hashlib,subprocess,fcntl,time,os,re
root=Path('/home/sean/funding-arb');sys.path.insert(0,str(root))
import paper_acceptance,recovery_a
rel=root/'releases/f16-core-aa02d6a';valid=json.loads((rel/'validation.json').read_text());assert valid['passed']
for name,h in valid['source_hashes'].items():
 assert hashlib.sha256((rel/name).read_bytes()).hexdigest()==h
 if name=='paper_notify.py':continue
 expected=(root/'releases/f16-churn-741edf0'/name).read_bytes()
 assert (root/name).read_bytes()==expected,name
unit=Path.home()/'.config/systemd/user/funding-arb-paper.service';original=unit.read_bytes();oldcodes={name:(root/name).read_bytes() for name in ('allocate_a.py','paper_a.py','paper_loop.py')}
old_dir=Path(re.search(r'--evidence-dir (\S+)',original.decode())[1]);old_contract=(old_dir/'contract.json').read_bytes();config=json.loads(old_contract)['runtime_config']
assert config['auto_recover'] and config['auto_exit'] and config['switch_policy']['min_hold_hours']=='24'
def system(*args):return subprocess.run(['systemctl','--user',*args],check=True,capture_output=True,text=True).stdout
def atomic(path,data):
 tmp=path.with_name(path.name+'.depth-fix-tmp');tmp.write_bytes(data);os.chmod(tmp,0o600 if path==unit else 0o644);os.replace(tmp,path)
notify_unit=unit.with_name('funding-arb-paper-notify.service');notify_original=notify_unit.read_bytes()
assert 'releases/paper-notify-f57c758e9b89/paper_notify.py' in notify_original.decode()
backup=rel/'backup';backup.mkdir()
with (root/'paper_a_state.lock').open('a') as lock:
 deadline=time.monotonic()+40
 while True:
  try:fcntl.flock(lock,fcntl.LOCK_EX|fcntl.LOCK_NB);break
  except BlockingIOError:
   if time.monotonic()>deadline:raise RuntimeError('LOCK_TIMEOUT_NO_CHANGE')
   time.sleep(.1)
 raw=(root/'paper_a_state.json').read_bytes();state=json.loads(raw);recovery_a.audit(state)
 assert not paper_acceptance.unfinished(state),'UNFINISHED_EXECUTION_NO_CHANGE'
 system('stop','funding-arb-paper.service','funding-arb-paper-notify.service')
 try:
  assert (root/'paper_a_state.json').read_bytes()==raw
  cursor=(root/'paper_notify_state.json').read_bytes()
  for name,data in list(oldcodes.items())+[('service-before.txt',original),('notify-service-before.txt',notify_original),('notify-cursor-at-switch.json',cursor),('paper-state-at-switch.json',raw)]:
   p=backup/name;p.write_bytes(data);os.chmod(p,0o600)
  for name in oldcodes:atomic(root/name,(rel/name).read_bytes())
  stamp=time.strftime('%Y%m%dT%H%M%SZ',time.gmtime());directory=root/'evidence'/('f16-core-observation-'+stamp)
  contract=paper_acceptance.begin(directory,state,72,120,config)
  changed=original.decode().replace(str(old_dir),str(directory)).encode();assert changed!=original
  atomic(unit,changed)
  notify_changed=notify_original.replace(b'releases/paper-notify-f57c758e9b89/paper_notify.py',b'releases/f16-core-aa02d6a/paper_notify.py')
  atomic(notify_unit,notify_changed);system('daemon-reload')
  assert (root/'paper_notify_state.json').read_bytes()==cursor
  assert (root/'paper_a_state.json').read_bytes()==raw
  assert (old_dir/'contract.json').read_bytes()==old_contract
  result={'release':str(rel),'directory':str(directory),'previous_directory':str(old_dir),'contract':contract,'end_not_before_ms':contract['started_at_ms']+72*3600000,'notification_cursor_unchanged_at_switch':True,'state_unchanged_at_switch':True,'state_sha256':hashlib.sha256(raw).hexdigest(),'old_sample_count':sum(1 for p in old_dir.glob('sample-*.json')),'unit_sha256':hashlib.sha256(changed).hexdigest(),'patch_sha256':valid['source_hashes']['check_a.py']}
  (rel/'activation.json').write_text(json.dumps(result,indent=2));(directory/'activation.json').write_text(json.dumps(result,indent=2))
 except BaseException:
  for name,data in oldcodes.items():atomic(root/name,data)
  atomic(unit,original);atomic(notify_unit,notify_original);system('daemon-reload');system('start','funding-arb-paper.service','funding-arb-paper-notify.service');raise
system('start','funding-arb-paper.service','funding-arb-paper-notify.service');print(json.dumps(result))
