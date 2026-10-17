"""Offline controls/publication state machine; no provider or remote mutation.

Publication fixtures isolate writer ordering; original126+7 retain all science,
binary, native, auth and payload validation. No production gate is modified.
"""
import copy
import hashlib
import importlib.util
import json
import os
from pathlib import Path
import sys
import tempfile
import types
import unittest
from unittest.mock import patch
from datetime import datetime, timezone

ROOT=Path(__file__).resolve().parents[2]
sys.path.insert(0,str(ROOT/'scripts'))
if sys.platform=='win32':
    # Unit-test only Linux contract seam. Actual hard ceiling remains tested
    # in Linux jobs; Windows does not implement the resource module.
    sys.modules['resource']=types.SimpleNamespace(RLIMIT_AS=9,getrlimit=lambda key:(2147483648,2147483648))
import atmos_production_runner as runner
import publisher_contract as contract
import publish_dataset as publisher

SHA='a'*40
REPO='shanestratton/thalassa-ocean-data'
NOW=datetime(2026,10,17,9,tzinfo=timezone.utc)

def environment():
    return {'GITHUB_SHA':SHA,'GITHUB_REPOSITORY':REPO,'GITHUB_REF':'refs/heads/master',
            'GITHUB_EVENT_NAME':'workflow_dispatch','GITHUB_ACTOR':'shanestratton',
            'GITHUB_WORKFLOW_REF':REPO+'/.github/workflows/atmos-reviewed-production-once.yml@refs/heads/master',
            'GITHUB_WORKFLOW_SHA':SHA,'GITHUB_RUN_ID':'123','GITHUB_RUN_ATTEMPT':'1'}


class ProductionContextTests(unittest.TestCase):
    def test_exact_new_writer_path_retains_all_concrete_workflow_admission_guards(self):
        source=(ROOT/'.github/workflows/atmos-reviewed-production-once.yml').read_text()
        self.assertEqual(source.count('default: false'),2)
        self.assertNotRegex(source,r'(?m)^\s*(?:schedule|push|pull_request|workflow_run):')
        self.assertNotIn('SOURCE_SHA:',source);self.assertNotIn('CONTROL_SHA:',source)
        self.assertEqual(source.count('contents: write'),1)
        for name in ('proof','generate','publish'):
            import re
            match=re.search(r'(?ms)^  '+name+r':\n(.*?)(?=^  [a-z]+:|\Z)',source)
            self.assertIsNotNone(match)
            block=match.group(1)
            for value in ("github.actor == 'shanestratton'","github.run_attempt == 1",
                          "github.repository == 'shanestratton/thalassa-ocean-data'",
                          "github.ref == 'refs/heads/master'","github.event_name == 'workflow_dispatch'",
                          'ref: ${{ github.sha }}','persist-credentials: false'):
                self.assertIn(value,block)
            if name!='publish':self.assertNotIn('contents: write',block)
        publish=re.search(r'(?ms)^  publish:\n(.*)\Z',source).group(1)
        for value in ('needs: generate','inputs.allow_provider_downloads == true',
                      'inputs.publish == true','contents: write',
                      'name: atmos141-production-bundle-${{ inputs.dataset }}-${{ github.run_id }}',
                      'scripts/publish_dataset.py --dataset "$ATMOS_DATASET"',
                      'test "$(git rev-parse HEAD)" = "$GITHUB_SHA"'):
            self.assertIn(value,publish)
        for value in ('pip install','SOURCE_SHA:','CONTROL_SHA:','COPERNICUS_'):
            self.assertNotIn(value,publish)
        self.assertEqual(hashlib.sha256((ROOT/'scripts/publish_dataset.py').read_bytes()).hexdigest(),
                         runner.PROVEN_FILES['scripts/publish_dataset.py'])
        spec=importlib.util.spec_from_file_location('_production_gate_fixture',ROOT/'scripts/publisher-tests/test_release_gate.py')
        gate=importlib.util.module_from_spec(spec);spec.loader.exec_module(gate)
        with tempfile.TemporaryDirectory() as directory:
            directory=Path(directory)
            for original in (ROOT/'.github/workflows').glob('*.yml'):
                (directory/original.name).write_text(original.read_text())
            with patch.object(gate,'WORKFLOWS_DIR',directory):
                check=gate.PublisherReleaseGateTests('test_every_workflow_is_least_privilege_pinned_and_on_one_runner')
                check.test_every_workflow_is_least_privilege_pinned_and_on_one_runner()
                reviewed=directory/'atmos-reviewed-production-once.yml'
                impostor=directory/'unreviewed-atmos-writer.yml'
                reviewed.rename(impostor)
                with self.assertRaises(AssertionError):
                    check.test_every_workflow_is_least_privilege_pinned_and_on_one_runner()
                impostor.rename(reviewed)
                reviewed.write_text(source.replace('contents: write','contents: write\n      issues: write'))
                with self.assertRaises(AssertionError):
                    check.test_every_workflow_is_least_privilege_pinned_and_on_one_runner()


    def test_new_workflow_and_real_master_producer_are_admitted_without_overrides(self):
        env=environment();before=dict(env)
        identity=runner.admit_context(env,SHA,True)
        self.assertEqual(identity['source'],SHA);self.assertEqual(identity['control'],SHA)
        self.assertEqual(env,before)
        draft={'producer':{'commit':SHA,'run_id':123,'run_attempt':1}}
        self.assertEqual(contract.validate_publish_context(draft,REPO,env),SHA)

    def test_wrong_sender_ref_repository_event_workflow_or_attempt_refuse(self):
        changes={'GITHUB_ACTOR':'other','GITHUB_REF':'refs/heads/feature','GITHUB_REPOSITORY':'other/repo',
                 'GITHUB_EVENT_NAME':'push','GITHUB_WORKFLOW_REF':REPO+'/.github/workflows/atmos-held-pipeline.yml@refs/heads/master',
                 'GITHUB_WORKFLOW_SHA':'b'*40,'GITHUB_RUN_ATTEMPT':'2','GITHUB_RUN_ID':'0'}
        for key,value in changes.items():
            env=environment();env[key]=value
            with self.subTest(key=key),self.assertRaises(contract.ContractError):runner.admit_context(env,SHA,True)

    def test_foreign_proof_identity_dirty_checkout_and_baseline_refuse(self):
        for key in ('SOURCE_SHA','CONTROL_SHA'):
            env=environment();env[key]=runner.SOURCE_SHA
            with self.assertRaises(contract.ContractError):runner.admit_context(env,SHA,True)
        with self.assertRaises(contract.ContractError):runner.admit_context(environment(),'b'*40,True)
        with self.assertRaises(contract.ContractError):runner.admit_context(environment(),SHA,False)
        env=environment();env['GITHUB_SHA']=runner.SOURCE_SHA
        with self.assertRaises(contract.ContractError):runner.admit_context(env,runner.SOURCE_SHA,True)

    def test_unchanged_publisher_rejects_foreign_commit_run_repo_ref_and_future_attempt(self):
        for change in ({'commit':'b'*40},{'run_id':456},{'run_attempt':2}):
            producer={'commit':SHA,'run_id':123,'run_attempt':1};producer.update(change)
            with self.assertRaises(contract.ContractError):contract.validate_publish_context({'producer':producer},REPO,environment())
        for key,value in [('GITHUB_REF','refs/heads/feature'),('GITHUB_REPOSITORY','other/repo')]:
            env=environment();env[key]=value
            with self.assertRaises(contract.ContractError):contract.validate_publish_context({'producer':{'commit':SHA,'run_id':123,'run_attempt':1}},REPO,env)

    def test_reviewed22_baseline_files_plus_narrow_gate_and_writer_are_exact(self):
        self.assertEqual(len(runner.PROVEN_FILES),24)
        runner.verify_proven_source(ROOT)

    def test_source_pin_drift_refuses(self):
        with tempfile.TemporaryDirectory() as temporary:
            root=Path(temporary);(root/'sample').write_bytes(b'changed')
            with patch.object(runner,'PROVEN_FILES',{'sample':hashlib.sha256(b'original').hexdigest()}):
                with self.assertRaises(contract.ContractError):runner.verify_proven_source(root)


class ProductionObserverTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup)
        self.root=Path(self.temp.name);self.run=datetime(2026,10,17,tzinfo=timezone.utc)
        self.identity=runner.admit_context(environment(),SHA,True)
        self.url='https://noaa-gfs-bdp-pds.s3.amazonaws.com/gfs.20261017/00/atmos/gfs.t00z.pgrb2.0p25.f000.idx'
    def budget(self):return runner.ObservedBudget('gfs-atmos',self.run,'aws',self.root,self.identity)
    def returned(self,budget,body):
        def read(port,url,host,*,byte_range=None):
            self.assertIs(port,budget);budget.requests+=1;budget.bytes+=len(body);return body
        return read
    def test_pass_through_exact_original_bytes_and_digest(self):
        budget=self.budget();body=b'original\x00\xff'
        with patch.object(runner.DownloadBudget,'read',self.returned(budget,body)):
            self.assertIs(budget.read(self.url,runner.GFS_HOST),body)
        row=json.loads((self.root/'observer.json').read_text())['records'][0]
        self.assertEqual(row['sha256'],hashlib.sha256(body).hexdigest());self.assertEqual(row['bytes'],len(body))
    def test_failed_partial_response_never_gets_complete_digest(self):
        budget=self.budget()
        def fail(*args,**kwargs):budget.requests+=1;budget.bytes+=17;raise OSError('x'*2000)
        with patch.object(runner.DownloadBudget,'read',side_effect=fail),self.assertRaises(OSError):budget.read(self.url,runner.GFS_HOST)
        row=budget.records[0];self.assertEqual(len(row['error']),512);self.assertNotIn('sha256',row);self.assertNotIn('bytes',row)
    def test_exact_source_urls_and_range_shape_refuse_before_parent(self):
        budget=self.budget()
        with patch.object(runner.DownloadBudget,'read') as parent:
            for url in (self.url+'?x=1',self.url.replace('20261017','20261018')):
                with self.assertRaises(contract.ContractError):budget.read(url,runner.GFS_HOST)
            for value in ((True,10),[1,10],(1,)):
                with self.assertRaises(contract.ContractError):budget.read(self.url.removesuffix('.idx'),runner.GFS_HOST,byte_range=value)
            parent.assert_not_called()
    def test_request211_holds_without_call_or_metadata_growth(self):
        budget=self.budget()
        with patch.object(runner.DownloadBudget,'read',self.returned(budget,b'x')):
            for _ in range(210):budget.read(self.url,runner.GFS_HOST)
            before=(self.root/'observer.json').read_bytes()
            with self.assertRaises(contract.ContractError):budget.read(self.url,runner.GFS_HOST)
        self.assertEqual(budget.requests,210);self.assertEqual((self.root/'observer.json').read_bytes(),before)
    def test_metadata_bomb_preserves_last_valid_receipt(self):
        path=self.root/'observer.json';runner.bounded_json(path,{'state':'valid'});before=path.read_bytes()
        with self.assertRaises(contract.ContractError):runner.bounded_json(path,{'large':'x'*(1024*1024)})
        self.assertEqual(path.read_bytes(),before)
    def test_google_profile_has_exact41_indices_and41_ranges(self):
        budget=runner.ObservedBudget('ifs-pressure',datetime(2026,10,16,12,tzinfo=timezone.utc),'google',self.root,self.identity)
        self.assertEqual(len(budget.allowed),82)
        self.assertTrue(all(host=='storage.googleapis.com' and url.startswith('https://storage.googleapis.com/ecmwf-open-data/20261016/12z/') for url,host,ranged in budget.allowed))
    def invoke(self,change=None,generate=None):
        class Clock(datetime):
            @classmethod
            def now(cls,zone):return NOW
        env={**environment(),'ATMOS_DATASET':'gfs-atmos','ATMOS_RUN_UTC':'2026-10-17T00:00:00+00:00','IFS_SOURCE':'aws','EVIDENCE_DIR':str(self.root),**(change or {})}
        signals=types.SimpleNamespace(SIGALRM=14,signal=lambda *args:None,alarm=lambda *args:None)
        with patch.dict(os.environ,env,clear=True),patch.object(runner,'git_identity',return_value=(SHA,True)),patch.object(runner,'verify_proven_source'),patch.object(runner,'datetime',Clock),patch.object(runner,'signal',signals),patch.object(runner.resource,'getrlimit',return_value=(2147483648,2147483648)),patch.object(runner,'generate',generate or (lambda *args,**kwargs:self.fail('no generator allowed'))):
            runner.main()
    def test_failed_generation_keeps_partial_frame_and_failure_receipt(self):
        def generate(dataset,run,output,**kwargs):
            output.mkdir();(output/'step-000.bin').write_bytes(b'partial');raise RuntimeError('native failure')
        with self.assertRaises(RuntimeError):self.invoke(generate=generate)
        self.assertEqual((self.root/'output/step-000.bin').read_bytes(),b'partial')
        self.assertFalse(json.loads((self.root/'observer.json').read_text())['outcome']['completeCohort'])
    def test_wrong_cycle_profile_and_sender_refuse_before_generate(self):
        for change in ({'ATMOS_RUN_UTC':'2026-10-17T06:00:00+00:00'},{'IFS_SOURCE':'google'},{'GITHUB_ACTOR':'other'}):
            with self.subTest(change=change),self.assertRaises(contract.ContractError):self.invoke(change)
        self.assertFalse((self.root/'output').exists())
    def test_original_caps_and_fixed_deadline_are_unchanged(self):
        self.assertEqual((runner.MAX_REQUESTS,runner.MAX_TOTAL_BYTES,runner.TOTAL_SECONDS),(210,512*1024*1024,1200))
        self.assertEqual(runner.CONTEXT['ifs-pressure'][1],'google')
        with self.assertRaises(TimeoutError):runner.expire(14,None)


class PublicationStateMachineTests(unittest.TestCase):
    def setUp(self):
        self.temp=tempfile.TemporaryDirectory();self.addCleanup(self.temp.cleanup);self.root=Path(self.temp.name)
        self.data=b'fictional-control-payload-not-science-proof'
        self.generation='g-20261017T000000Z-0123456789ab'
        self.filename=self.generation+'-h000.bin'
        (self.root/self.filename).write_bytes(self.data)
        self.draft={'dataset':{'key':'gfs-atmos'},'generation':self.generation,'generated_at':'2026-10-17T08:00:00Z',
                    'published_at':None,'data_start':'2026-10-17T00:00:00Z','data_end':'2026-10-24T00:00:00Z',
                    'producer':{'commit':SHA,'run_id':123,'run_attempt':1},
                    'files':[{'filename':self.filename,'bytes':len(self.data),'sha256':hashlib.sha256(self.data).hexdigest()}]}
        self.events=[];self.downloads=[]
    def publish(self,inventory=None,remote=None,fresh=None):
        def gh(args,**kwargs):self.events.append(('asset',args[2],Path(args[3]).name))
        def download(tag,repo,name,path):
            if name==publisher.LEGACY_MANIFEST_NAME:return False
            self.downloads.append(name);path.write_bytes(self.data if remote is None else remote);return True
        def slot(tag,repo,name,candidate,*args):
            self.events.append(('slot',name,json.loads(candidate.read_text())['published_at']))
        class Clock(datetime):
            @classmethod
            def now(cls,zone):return NOW
        with patch.dict(os.environ,{**environment(),'GH_TOKEN':'offline-not-a-token'},clear=True),patch.object(publisher,'load_manifest',return_value=copy.deepcopy(self.draft)),patch.object(publisher,'validate_manifest'),patch.object(publisher,'validate_bundle_layout'),patch.object(publisher,'validate_public_repository'),patch.object(publisher,'read_manifest_slots',return_value=({},set())),patch.object(publisher,'download_asset',side_effect=download),patch.object(publisher,'release_assets',return_value=inventory),patch.object(publisher,'ensure_release',side_effect=lambda *args,**kwargs:self.events.append(('release',args[0]))),patch.object(publisher,'run_gh',side_effect=gh),patch.object(publisher,'upload_manifest_slot',side_effect=slot),patch.object(publisher,'validate_publication_freshness',side_effect=fresh),patch.object(publisher,'datetime',Clock):
            publisher.publish('gfs-atmos',self.root,REPO)
    def test_bootstrap_assets_are_verified_before_both_v2_slots_last(self):
        self.publish()
        self.assertEqual([e[0] for e in self.events],['release','release','asset','slot','slot'])
        self.assertEqual([e[1] for e in self.events[-2:]],['manifest-v2-a.json','manifest-v2-b.json'])
        self.assertEqual(self.downloads,[self.filename])
        self.assertNotIn('manifest.json',[e[1] for e in self.events])
    def test_existing_size_collision_refuses_before_any_mutation(self):
        with self.assertRaisesRegex(contract.ContractError,'size collision'):self.publish(inventory=[{'name':self.filename,'size':1}])
        self.assertEqual(self.events,[])
    def test_existing_checksum_collision_refuses_before_any_mutation(self):
        with self.assertRaisesRegex(contract.ContractError,'hash collision'):self.publish(inventory=[{'name':self.filename,'size':len(self.data)}],remote=b'bad')
        self.assertEqual(self.events,[])
    def test_uploaded_checksum_failure_never_publishes_slots(self):
        with self.assertRaisesRegex(contract.ContractError,'verification failed'):self.publish(remote=b'bad')
        self.assertFalse(any(e[0]=='slot' for e in self.events))
    def test_shard900_is_exact_and901_refuses(self):
        inventory=[{'name':f'g-20261017T000000Z-{i:012x}-h000.bin','size':1} for i in range(900)]
        tag=contract.asset_shard_tag('gfs-atmos',self.generation)
        self.assertEqual(len(contract.validate_shard_inventory('gfs-atmos',tag,inventory,[])),900)
        with self.assertRaisesRegex(contract.ContractError,'900-asset'):contract.validate_shard_inventory('gfs-atmos',tag,inventory,self.draft['files'])
        with self.assertRaisesRegex(contract.ContractError,'does not belong'):contract.validate_shard_inventory('gfs-atmos','gfs-atmos-latest-assets-2026-W41',inventory,[])
    def test_final_switch_freshness_refusal_leaves_both_slots_unwritten(self):
        calls=[]
        def fresh(value):
            calls.append(True)
            if len(calls)==3:raise contract.ContractError('freshness expired at final switch')
        with self.assertRaisesRegex(contract.ContractError,'freshness expired'):self.publish(fresh=fresh)
        self.assertEqual(len(calls),3);self.assertFalse(any(e[0]=='slot' for e in self.events))
    def test_real_freshness_gate_refuses_expired_source_and_old_generation(self):
        contract.validate_publication_freshness(self.draft,now=NOW)
        for field,value in [('data_start','2026-10-16T00:00:00Z'),('generated_at','2026-10-17T01:00:00Z')]:
            draft=copy.deepcopy(self.draft);draft[field]=value
            with self.assertRaises(contract.ContractError):contract.validate_publication_freshness(draft,now=NOW)


if __name__=='__main__':unittest.main(verbosity=2)
