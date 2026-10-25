from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch
from skill_evolver.budget import Budget
from skill_evolver.config import load_config, validate_config
from skill_evolver.evolution import Experiment
from skill_evolver.genome import candidate
from skill_evolver.model import ModelError
from skill_evolver.report import build_report
from test_evolution import FakeRunner

CONFIG = Path(__file__).resolve().parents[1] / 'experiments/pilot/config.json'


class Broker:
    def __init__(self, error=None, invalid=False):
        self.calls = []
        self.error, self.invalid = error, invalid

    def complete(self, context, **limits):
        self.calls.append((context, limits))
        if self.error:
            raise self.error
        metadata = context['original_frontmatter']
        text = metadata + context['current_body'] + '\nCheck the output.\n'
        if self.invalid:
            text = text.replace('name: uppercase-file','name: changed')
        return {'content':{'skill_text':text},'input_tokens':10,'output_tokens':10,'tokens':20}


class ProposerTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(CONFIG)
        self.config['search'].update(population=3, generations=1)
        for role in ['model','proposal_model']:
            self.config[role] = {'provider':'openai','id':'configured-model','options':{},'api_key_env':'SKILL_EVOLVER_TEST_UNUSED_KEY','count_fee_acknowledged':True}
        self.config['pricing'] = {'version':'configured-token-rate-test','per_token':0.00001}
        self.original = candidate(Path(self.config['skill_path']).read_text())

    def test_config_contract_no_key_required_for_validate(self):
        validate_config(self.config)
        for field,value in [('options',{'temperature':1}),('api_key_env',''),('count_fee_acknowledged',False),('endpoint','https://example.com')]:
            config = load_config(CONFIG)
            config['model'] = dict(self.config['model'], **{field:value})
            config['pricing'] = self.config['pricing']
            with self.assertRaises(ValueError):
                validate_config(config)

    def test_paid_proposal_persistence_replay_and_search_only_context(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = Broker()
            experiment = Experiment(self.config,directory,FakeRunner(),broker)
            experiment.initialize()
            for phase in ['search','selection','final']:
                experiment.store.append({'kind':'trial','id':phase,'phase':phase,'method':'ga','search':0,'tokens':0,'evaluation':{'score':0},'case_id':phase,'transcript':[phase+'-marker'],'expected':'NEVER_EXPOSE'})
            donor = candidate(self.original['text'].replace('Use tr', 'First use tr'))
            child = experiment.real_propose(self.original,'ga',0,'one',donor)
            context, limits = broker.calls[0]
            self.assertIn('First use tr',context['current_body'])
            self.assertEqual(context['search_failures'][0]['transcript'],['search-marker'])
            self.assertNotIn('NEVER_EXPOSE',str(context))
            self.assertNotIn('selection-marker',str(context))
            self.assertNotIn('final-marker',str(context))
            self.assertEqual(child['parents'],[self.original['id'],donor['id']])
            replay = Experiment(self.config,directory,FakeRunner(),Broker(RuntimeError('must not call')))
            replay.initialize()
            self.assertEqual(replay.real_propose(self.original,'ga',0,'one',donor)['id'], child['id'])
            self.assertEqual(experiment.budget('ga',0,'search',proposal=True).usage()['tokens'],20)
            self.assertAlmostEqual(experiment.budget('ga',0,'search',proposal=True).usage()['money'],0.0002)

    def test_invalid_mutation_paid_and_known_unknown_errors(self):
        for broker, expected_tokens in [(Broker(invalid=True),20),(Broker(ModelError('preflight',tokens=0)),0),(Broker(ModelError('unknown',tokens=None)),9216)]:
            with self.subTest(expected_tokens=expected_tokens), tempfile.TemporaryDirectory() as directory:
                experiment = Experiment(self.config,directory,FakeRunner(),broker)
                experiment.initialize()
                self.assertIsNone(experiment.real_propose(self.original,'ga',0,'one'))
                self.assertEqual(experiment.budget('ga',0,'search',proposal=True).usage()['tokens'],expected_tokens)
                experiment.real_propose(self.original,'ga',0,'one')
                self.assertEqual(len(broker.calls),1)

    def test_interruption_and_programming_error_never_regenerate(self):
        for error in [KeyboardInterrupt(),RuntimeError('bug'), ValueError('unexpected broker bug')]:
            with tempfile.TemporaryDirectory() as directory:
                experiment = Experiment(self.config,directory,FakeRunner(),Broker(error))
                experiment.initialize()
                with self.assertRaises(type(error)):
                    experiment.real_propose(self.original,'ga',0,'one')
                resumed_broker = Broker()
                resumed = Experiment(self.config,directory,FakeRunner(),resumed_broker)
                resumed.initialize()
                self.assertIsNone(resumed.real_propose(self.original,'ga',0,'one'))
                self.assertEqual(len(resumed_broker.calls),0)
                self.assertEqual(resumed.budget('ga',0,'search',proposal=True).usage()['tokens'],9216)

    def test_result_before_settlement_recovers_exact_candidate(self):
        with tempfile.TemporaryDirectory() as directory:
            experiment = Experiment(self.config,directory,FakeRunner(),Broker())
            experiment.initialize()
            with patch.object(Budget,'settle',side_effect=KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    experiment.real_propose(self.original,'ga',0,'one')
            resumed = Experiment(self.config,directory,FakeRunner(),Broker(RuntimeError('must not call')))
            resumed.initialize()
            child = resumed.real_propose(self.original,'ga',0,'one')
            self.assertTrue((Path(directory)/'candidates'/(child['id']+'.json')).exists())
            self.assertEqual(resumed.budget('ga',0,'search',proposal=True).usage()['tokens'],20)

    def test_fake_real_search_report_no_small_group_claim(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertTrue(Experiment(self.config,directory,FakeRunner(),Broker()).run()['complete'])
            report = build_report(directory)
            self.assertFalse(report['mock_only'])
            self.assertIn('Superiority unverified',report['searches'][0]['judgment'])
            self.assertIn('invoice',report['accounting_caveat'])

    def test_mixed_role_recovery_preserves_reservation_rates(self):
        self.config['model'] = load_config(CONFIG)['model']
        with tempfile.TemporaryDirectory() as directory:
            experiment = Experiment(self.config, directory, FakeRunner(), Broker())
            experiment.initialize()
            case = self.config['splits']['search'][0]
            with patch.object(Budget, 'settle', side_effect=KeyboardInterrupt()):
                with self.assertRaises(KeyboardInterrupt):
                    experiment.trial(self.original, case, 0, 'ga', 0, 'search')
                with self.assertRaises(KeyboardInterrupt):
                    experiment.real_propose(self.original, 'ga', 0, 'one')
            resumed = Experiment(self.config, directory, FakeRunner(), Broker(RuntimeError('must not call')))
            resumed.initialize()
            settlements = [event for event in resumed.store.events() if event['kind'] == 'settle']
            self.assertEqual(len(settlements), 2)
            trial = next(event for event in settlements if not event['id'].startswith('proposal/'))
            proposal = next(event for event in settlements if event['id'].startswith('proposal/'))
            self.assertEqual((trial['tokens'], trial['rate'], trial['money']), (10, 0, 0))
            self.assertEqual(proposal['tokens'], 20)
            self.assertEqual(proposal['rate'], self.config['pricing']['per_token'])
            self.assertAlmostEqual(proposal['money'], 0.0002)
            self.assertAlmostEqual(resumed.budget('ga', 0, 'search').usage()['money'], 0.0002)

    def test_unknown_proposal_invalidates_search_and_replay(self):
        with tempfile.TemporaryDirectory() as directory:
            broker = Broker(ModelError('unknown',tokens=None))
            result = Experiment(self.config,directory,FakeRunner(),broker).run()
            self.assertFalse(result['complete'])
            self.assertFalse(result['validity']['ga/0']['valid'])
            report = build_report(directory)
            self.assertEqual(report['searches'][0]['ga_minus'],{})
            self.assertNotIn('Evidence of improvement',report['searches'][0]['judgment'])
            resumed_broker = Broker()
            self.assertFalse(Experiment(self.config,directory,FakeRunner(),resumed_broker).run()['complete'])
            self.assertEqual(resumed_broker.calls,[])
