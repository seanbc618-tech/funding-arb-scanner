import sys,copy,json,tempfile,unittest
from unittest.mock import patch
from pathlib import Path
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import f14_f15 as rotation_fixture
import f16_acceptance as acceptance_fixture
import rotate_a, paper_a, paper_acceptance, recovery_a, paper_replay

class Review(unittest.TestCase):
 def test_requote_cannot_spend_forecast_instead_of_actual_exit_proceeds(self):
  c=rotation_fixture.RotationRecovery();c.setUp();self.addCleanup(c.doCleanups)
  c.run_rotate();c.advance();c.run_rotate()
  original=c.c.ex.fetch_order_book
  def adverse_book(symbol,limit=50):
   b=original(symbol,limit)
   factor=paper_a.dec('.98') if ':' not in symbol else paper_a.dec('1.02')
   for side in ('asks','bids'):
    b[side]=[[str(paper_a.dec(px)*factor),qty] for px,qty in b[side]]
   return b
  c.c.ex.fetch_order_book=adverse_book
  paper_a.operate(c.c.s,c.c.path,c.c.ex,'BTC',closing=True)
  c.c.ex.fetch_order_book=original
  c.run_rotate();c.advance()
  result=c.run_rotate()
  self.assertNotEqual(result['reason'],'SWITCH_COMPLETED')
  self.assertFalse(any(t['coin']=='ETH' for t in c.c.s['trades'].values()))
 def test_unknown_final_execution_cannot_pass_completed_observation(self):
  c=acceptance_fixture.Acceptance();c.setUp();self.addCleanup(c.doCleanups)
  c.test_complete_linked_cycle_can_pass_declared_observation()
  paths=sorted(c.path.glob('sample-*.json'));sample=json.loads(paths[-1].read_text())
  state=json.loads((c.path/f"state-{sample['state_sha256']}.json").read_text())
  state['pending']={'order_id':'uncommitted','trade_id':next(iter(state['trades'])),'reserved_usdt':'1'}
  paper_acceptance.time.time.return_value+=1
  report=c.report();report['decision']='ROTATION_PROCESSED'
  paper_acceptance.record(c.path,state,report)
  result=paper_acceptance.assess(c.path)
  self.assertNotEqual(result['status'],'PASS_PAPER_OBSERVATION')
  self.assertIsNone(result['metrics']['closed_net_usdt'])
 def test_no_position_record_requires_zero_holding_evidence(self):
  c=acceptance_fixture.Acceptance();c.setUp();self.addCleanup(c.doCleanups)
  d=acceptance_fixture.dataset();paper_replay.replay(d,c.path)
  state=json.loads((c.path/'paper.json').read_text());entry=next(e for e in state['funding'].values() if e['status']=='POSTED')
  state['cash_usdt']=str(paper_a.dec(state['cash_usdt'])-paper_a.dec(entry['payment_usdt']))
  entry.update(status='NO_POSITION',payment_usdt='0')
  paper_acceptance.time.time.return_value=d['frames'][-1]['at_ms']/1000
  with self.assertRaisesRegex(ValueError,'FUNDING'):recovery_a.audit(state)
 def test_observation_freezes_accounting_dependency(self):
  before=paper_acceptance.code_digest();original=Path.read_bytes
  def changed(path):
   data=original(path)
   return data+b'\n# changed accounting implementation\n' if path.name=='accounting.py' else data
  with patch.object(Path,'read_bytes',changed):after=paper_acceptance.code_digest()
  self.assertNotEqual(before,after)
if __name__=='__main__':unittest.main(verbosity=2)
