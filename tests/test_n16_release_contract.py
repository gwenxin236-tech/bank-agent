"""Publication wording must follow validated current sources, never magic scores."""
import copy,hashlib,tempfile,unittest
from pathlib import Path
import source_release as sr
class MaterialEvidenceTests(unittest.TestCase):
 def fixture(self,root):
  (root/'app').mkdir();f=root/'app/main.py';f.write_text('pass',encoding='utf-8')
  return {'regression':{'current_full_windows':{'status':'PASS','tests':17,'skipped':0,'resource_warnings':0,'failures':0,'errors':0},'current_core_linux':{'status':'NOT_RUN_DOCKER_BROKEN'}},'offline':{'status':'PENDING_DOCKER_ENGINE'},'source_files':{'app/main.py':hashlib.sha256(f.read_bytes()).hexdigest()}}
 def test_valid_current_evidence_accepts_any_positive_test_count_without_promoting_docker(self):
  with tempfile.TemporaryDirectory() as tmp:
   r=Path(tmp);e=self.fixture(r);self.assertEqual(sr.material_evidence(r,e)['regression']['current_full_windows']['tests'],17);self.assertEqual(e['offline']['status'],'PENDING_DOCKER_ENGINE')
 def test_stale_sources_skips_and_resource_warnings_refuse_material_claims(self):
  with tempfile.TemporaryDirectory() as tmp:
   r=Path(tmp);e=self.fixture(r)
   for key in ('skipped','resource_warnings','failures','errors'):
    bad=copy.deepcopy(e);bad['regression']['current_full_windows'][key]=1
    with self.subTest(key=key),self.assertRaises(ValueError):sr.material_evidence(r,bad)
   (r/'app/main.py').write_text('changed',encoding='utf-8')
   with self.assertRaises(ValueError):sr.material_evidence(r,e)
 def test_runtime_pass_requires_linux_and_all_three_profile_receipts(self):
  with tempfile.TemporaryDirectory() as tmp:
   r=Path(tmp);e=self.fixture(r);e['offline']['status']='PASS'
   with self.assertRaises(ValueError):sr.material_evidence(r,e)
   e['regression']['current_core_linux']={'status':'PASS'};e['offline']['profiles']={k:{'status':'PASS'} for k in ('standard','identity','fineract')}
   self.assertIs(sr.material_evidence(r,e),e)
if __name__=='__main__':unittest.main()
