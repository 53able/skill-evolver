from pathlib import Path
import json
import tempfile
import unittest
from skill_evolver.config import load_config, trial_reservation
from skill_evolver.evolution import Experiment
from skill_evolver.report import build_report, paired_interval
from skill_evolver.store import Store
from skill_evolver.budget import Budget
from skill_evolver.genome import candidate

CONFIG = Path(__file__).resolve().parents[1] / 'experiments/pilot/config.json'


class FakeRunner:
    def __init__(self, fail=None):
        self.calls = []
        self.fail = fail
        self.resource_recorder = None
        self.prepared = []
        self.recovered = []

    def prepare_environment(self, pinned_image_id=None):
        self.prepared.append(pinned_image_id)
        return pinned_image_id or 'sha256:' + 'f' * 64

    def recover_resources(self, events):
        self.recovered.append(events)

    def run(self, skill_text, case, *, trial_id):
        assert 'expected' not in case
        self.calls.append(trial_id)
        if self.fail:
            raise self.fail
        return {'status':'success','outputs':{case['output_paths'][0]:next(iter(case['files'].values())).upper()},'tokens':10,'duration_seconds':0.01,'termination_reason':'finished','model':{'provider':'mock','id':'fake','options':{}},'environment_hash':'fake-env','skill_loaded':True,'transcript':[]}


class EvolutionTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(CONFIG)
        self.config['search']['population'] = 3
        self.config['search']['generations'] = 2

    def test_complete_replay_cache_and_phase_barriers(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            result = Experiment(self.config, directory, runner).run()
            self.assertTrue(result['complete'])
            calls = len(runner.calls)
            self.assertTrue(Experiment(self.config, directory, runner).run()['complete'])
            self.assertEqual(len(runner.calls), calls)
            events = Store(directory).events()
            first_selection = next(i for i,e in enumerate(events) if e.get('phase')=='selection')
            self.assertEqual(sum(e['kind']=='frozen' for e in events[:first_selection]),3)
            first_final = next(i for i,e in enumerate(events) if e.get('phase')=='final')
            self.assertEqual(sum(e['kind']=='winner' for e in events[:first_final]),3)
            report = build_report(directory)
            self.assertTrue(report['mock_only'])
            self.assertTrue(all(m['complete'] for m in report['searches'][0]['methods'].values()))
            # All methods pay for their own original evaluation (no cross-method cache).
            original = candidate(Path(self.config['skill_path']).read_text())['id']
            for method in ['ga','sequential','original']:
                self.assertTrue(any('/'+original+'/' in c and c.startswith(method+'/0/search/') for c in runner.calls))

    def test_interruption_retains_reservation(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner(KeyboardInterrupt())
            with self.assertRaises(KeyboardInterrupt):
                Experiment(self.config,directory,runner).run()
            store = Store(directory)
            reserved = next(e for e in store.events() if e['kind']=='reserve')
            runner = FakeRunner()
            Experiment(self.config,directory,runner).run()
            self.assertNotIn(reserved['id'],runner.calls)
            self.assertTrue(any(e.get('status')=='indeterminate' for e in store.events()))
            self.assertEqual(Budget(store,reserved['scope'],self.config['budgets']['search']).records()[reserved['id']]['tokens'],trial_reservation(self.config))

    def test_partial_candidate_not_ranked(self):
        self.config['budgets']['search']['tokens'] = trial_reservation(self.config)
        class FullUsage(FakeRunner):
            def run(inner,*args,**kwargs):
                result = super().run(*args,**kwargs)
                result['tokens'] = trial_reservation(self.config)
                return result
        with tempfile.TemporaryDirectory() as directory:
            result = Experiment(self.config,directory,FullUsage()).run()
            self.assertFalse(result['complete'])
            self.assertTrue(all(not e['candidates'] for e in Store(directory).events() if e['kind']=='frozen'))

    def test_group_bootstrap_case_weighting_and_small_group(self):
        rows = []
        for method in ['ga','original']:
            for case,group in [('a','one'),('b','one'),('c','two')]:
                for repeat in range(3):
                    score = int(method=='ga' and case!='c')
                    rows.append({'method':method,'search':0,'case_id':case,'group_id':group,'evaluation':{'score':score}})
        result = paired_interval(rows,'ga','original',0,iterations=1000)
        self.assertAlmostEqual(result['difference'],2/3)
        self.assertTrue(result['exploratory'])
        self.assertEqual(result['groups'],2)
        self.assertEqual(result['interval'],[0,1])

    def test_final_interruption_visible_in_report(self):
        class InterruptFinal(FakeRunner):
            def run(inner, *args, **kwargs):
                if '/final/' in kwargs['trial_id']:
                    raise KeyboardInterrupt()
                return super().run(*args, **kwargs)
        with tempfile.TemporaryDirectory() as directory:
            with self.assertRaises(KeyboardInterrupt):
                Experiment(self.config,directory,InterruptFinal()).run()
            Experiment(self.config,directory,FakeRunner()).run()
            report = build_report(directory)
            unknown = [r for r in report['all_trials'] if r['status']=='indeterminate']
            self.assertEqual(len(unknown),1)
            self.assertEqual(unknown[0]['phase'],'final')
            for key in ['experiment_id','method','search','candidate_id','case_id','group_id','repeat','model','environment_hash','duration_seconds']:
                self.assertIn(key,unknown[0])
            self.assertFalse(report['searches'][0]['methods']['original']['complete'])
            self.assertTrue(any(r['status']=='indeterminate' for r in report['searches'][0]['methods']['original']['cases']))


class ReviewRegressionTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(CONFIG)
        self.config['search'].update(population=3,generations=1)
        self.original = candidate(Path(self.config['skill_path']).read_text())

    def test_distinct_cases_never_share_trial_or_charge(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            experiment = Experiment(self.config,directory,runner)
            experiment.initialize()
            results = [experiment.trial(self.original,case,0,'ga',0,'search') for case in self.config['splits']['search'][:2]]
            self.assertNotEqual(results[0]['id'],results[1]['id'])
            self.assertEqual(len(runner.calls),2)
            self.assertEqual(experiment.budget('ga',0,'search').usage()['tokens'],20)

    def test_exhausted_search_infrastructure_cannot_complete_or_resume_valid(self):
        original_id = self.original['id']
        class BrokenCandidates(FakeRunner):
            def run(inner,*args,**kwargs):
                result = super().run(*args,**kwargs)
                if '/search/' in kwargs['trial_id'] and original_id not in kwargs['trial_id']:
                    result.update(status='infrastructure_error',termination_reason='injected')
                return result
        with tempfile.TemporaryDirectory() as directory:
            result = Experiment(self.config,directory,BrokenCandidates()).run()
            self.assertFalse(result['complete'])
            self.assertFalse(result['validity']['ga/0']['valid'])
            self.assertFalse(Experiment(self.config,directory,FakeRunner()).run()['complete'])
            report = build_report(directory)
            self.assertFalse(report['searches'][0]['methods']['ga']['complete'])
            self.assertEqual(report['searches'][0]['ga_minus'],{})
            for event in Store(directory).events():
                if event['kind'] in ('frozen','winner'):
                    self.assertIn('validity',event)

    def test_successful_retry_clears_only_resolved_infrastructure(self):
        class RetryOnce(FakeRunner):
            def run(inner,*args,**kwargs):
                result = super().run(*args,**kwargs)
                if '/search/' in kwargs['trial_id'] and kwargs['trial_id'].endswith('/0'):
                    result.update(status='infrastructure_error',termination_reason='transient')
                return result
        with tempfile.TemporaryDirectory() as directory:
            result = Experiment(self.config,directory,RetryOnce()).run()
            self.assertTrue(result['complete'])
            self.assertTrue(all(v['valid'] for v in result['validity'].values()))
            self.assertTrue(all(m['complete'] for m in build_report(directory)['searches'][0]['methods'].values()))

    def test_budget_only_stop_allows_original_fallback(self):
        amount = trial_reservation(self.config)
        self.config['budgets']['search']['tokens'] = 3 * amount
        class FullUsage(FakeRunner):
            def run(inner,*args,**kwargs):
                result = super().run(*args,**kwargs)
                result['tokens'] = amount
                return result
        with tempfile.TemporaryDirectory() as directory:
            result = Experiment(self.config,directory,FullUsage()).run()
            self.assertTrue(result['complete'])
            self.assertTrue(all(v['valid'] for v in result['validity'].values()))
            self.assertTrue(all([c['id'] for c in e['candidates']]==[self.original['id']] for e in Store(directory).events() if e['kind']=='frozen'))

    def test_runtime_freeze_resume_and_resource_recovery_contract(self):
        with tempfile.TemporaryDirectory() as directory:
            runner = FakeRunner()
            experiment = Experiment(self.config,directory,runner)
            self.assertTrue(experiment.run()['complete'])
            manifest = json.loads((Path(directory)/'experiment.json').read_text())
            self.assertEqual(manifest['runtime_image_id'],'sha256:'+'f'*64)
            self.assertEqual(manifest['environment_hash'],'fake-env')
            experiment.store.append({'kind':'resource_allocated','resource_id':'nonce','trial_id':'interrupted'})
            resumed = FakeRunner()
            self.assertTrue(Experiment(self.config,directory,resumed).run()['complete'])
            self.assertEqual(resumed.prepared,[manifest['runtime_image_id']])
            self.assertTrue(any(e['kind']=='resource_allocated' for e in resumed.recovered[0]))
            self.assertTrue(callable(resumed.resource_recorder))
            changed = dict(self.config,experiment_id='different')
            rejected = FakeRunner()
            with self.assertRaises(ValueError):
                Experiment(changed,directory,rejected).run()
            self.assertEqual(rejected.prepared,[])
            self.assertEqual(rejected.recovered,[])

    def test_drift_rejected_and_report_recomputes_old_uncertainty(self):
        class Drift(FakeRunner):
            def run(inner,*args,**kwargs):
                result = super().run(*args,**kwargs)
                if len(inner.calls)>1:
                    result['environment_hash']='changed-environment'
                return result
        with tempfile.TemporaryDirectory() as directory:
            result = Experiment(self.config,directory,Drift()).run()
            self.assertFalse(result['complete'])
            report = build_report(directory)
            self.assertFalse(report['runtime_validity']['valid'])
            self.assertTrue(all(not m['complete'] for m in report['searches'][0]['methods'].values()))
            with self.assertRaises(ValueError):
                Experiment(self.config,directory,FakeRunner()).run()
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(Experiment(self.config,directory,FakeRunner()).run()['complete'])
            Store(directory).append({'kind':'trial','id':'ga/0/search/old-candidate/old-case/0/1','scope':'ga/0/search','status':'infrastructure_error','evaluation':{'complete':False,'score':None},'environment_hash':'fake-env'})
            report = build_report(directory)
            self.assertFalse(report['searches'][0]['methods']['ga']['complete'])
            self.assertEqual(report['searches'][0]['ga_minus'],{})

    def test_report_blocks_otherwise_positive_real_comparison_from_old_uncertainty(self):
        self.config['model'] = self.config['proposal_model'] = {'provider':'openai','id':'test-model','options':{},'api_key_env':'UNUSED_TEST_KEY','count_fee_acknowledged':True}
        self.config['pricing'] = {'version':'fake','per_token':0.00001}
        self.config['splits']['final'] = [dict(self.config['splits']['final'][0],id=f'final-{i}',group_id=f'final-group-{i}') for i in range(10)]
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.write('experiment.json',{'config':self.config,'runtime_image_id':'sha256:'+'f'*64,'environment_hash':'fake-env'})
            for method in ['original','sequential','ga']:
                for case in self.config['splits']['final']:
                    for repeat in range(3):
                        store.append({'kind':'trial','id':f'{method}/0/final/candidate/{case["id"]}/{repeat}/0',
                                      'scope':f'{method}/0/final','method':method,'search':0,'phase':'final',
                                      'case_id':case['id'],'group_id':case['group_id'],'repeat':repeat,
                                      'status':'success','environment_hash':'fake-env',
                                      'evaluation':{'complete':True,'score':int(method=='ga')}})
            self.assertIn('Evidence of improvement',build_report(directory)['searches'][0]['judgment'])
            for attempt, status, tokens in [(0,'infrastructure_error',None),(1,'success',10)]:
                store.append({'kind':'trial','id':f'ga/0/search/old-candidate/old-case/0/{attempt}',
                              'scope':'ga/0/search','status':status,'tokens':tokens,
                              'environment_hash':'fake-env','evaluation':{'complete':status=='success','score':1 if status=='success' else None}})
            report = build_report(directory)
            self.assertFalse(report['complete'])
            self.assertNotIn('Evidence of improvement',report['searches'][0]['judgment'])
            self.assertEqual(report['searches'][0]['ga_minus'],{})
            # Historical event without modern method/phase metadata still poisons validity.
            store.append({'kind':'proposal_result','id':'proposal/ga/0/old','status':'indeterminate','tokens':None,'candidate':None})
            report = build_report(directory)
            self.assertFalse(report['complete'])
            self.assertNotIn('Evidence of improvement',report['searches'][0]['judgment'])
            self.assertEqual(report['searches'][0]['ga_minus'],{})

    def test_resume_rejects_missing_or_changed_runtime_pin_before_new_trials(self):
        with tempfile.TemporaryDirectory() as directory:
            Experiment(self.config,directory,FakeRunner()).run()
            class ChangedPin(FakeRunner):
                def prepare_environment(inner,pinned_image_id=None):
                    return 'sha256:'+'a'*64
            rejected = ChangedPin()
            with self.assertRaises(ValueError):
                Experiment(self.config,directory,rejected).run()
            self.assertEqual(rejected.calls,[])
            self.assertEqual(rejected.recovered,[])
            path = Path(directory)/'experiment.json'
            manifest = json.loads(path.read_text())
            del manifest['runtime_image_id']
            path.write_text(json.dumps(manifest))
            with self.assertRaisesRegex(ValueError,'historical experiment'):
                Experiment(self.config,directory,FakeRunner()).run()
            self.assertFalse(build_report(directory)['complete'])

    def test_report_preserves_legacy_fieldless_final_indeterminate(self):
        with tempfile.TemporaryDirectory() as directory:
            Experiment(self.config,directory,FakeRunner()).run()
            store = Store(directory)
            final = next(e for e in store.events() if e['kind']=='trial' and e.get('phase')=='final')
            store.append({'kind':'trial','id':final['id'],'scope':final['scope'],'status':'indeterminate',
                          'evaluation':{'score':None,'complete':False},'tokens':None})
            report = build_report(directory)
            self.assertFalse(report['complete'])
            self.assertTrue(any(row['status']=='indeterminate' for row in report['searches'][0]['methods']['original']['cases']))

    def test_unknown_infrastructure_usage_survives_successful_retry_and_resume(self):
        class UnknownThenSuccess(FakeRunner):
            def run(inner,*args,**kwargs):
                result = super().run(*args,**kwargs)
                if len(inner.calls) == 1:
                    result.update(status='infrastructure_error',tokens=None,
                                  termination_reason='sandbox cleanup failed')
                return result
        with tempfile.TemporaryDirectory() as directory:
            runner = UnknownThenSuccess()
            experiment = Experiment(self.config,directory,runner)
            result = experiment.run()
            events = experiment.store.events()
            unknown = next(e for e in events if e['kind']=='trial' and e['tokens'] is None)
            planned = unknown['id'].rsplit('/',1)[0]
            self.assertTrue(any(e['kind']=='trial' and e['id']==planned+'/1' and e['status']=='success' for e in events))
            self.assertFalse(result['complete'])
            self.assertFalse(result['validity']['original/0']['valid'])
            report = build_report(directory)
            self.assertFalse(report['complete'])
            self.assertEqual(report['searches'][0]['ga_minus'],{})
            self.assertNotIn('Evidence of improvement',report['searches'][0]['judgment'])
            self.assertEqual(sum(v['unsettled_reservations'] for v in report['budgets'].values()),1)
            charge = experiment.budget('original',0,'search').records()[unknown['id']]
            self.assertEqual(charge['kind'],'reserve')
            self.assertEqual(charge['tokens'],trial_reservation(self.config))
            resumed = FakeRunner()
            self.assertFalse(Experiment(self.config,directory,resumed).run()['complete'])
            self.assertEqual(resumed.calls,[])
            self.assertEqual(sum(v['unsettled_reservations'] for v in build_report(directory)['budgets'].values()),1)
