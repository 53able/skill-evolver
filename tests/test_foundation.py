import copy
from pathlib import Path
import random
import tempfile
import unittest
from skill_evolver.config import load_config, validate_config, public_case
from skill_evolver.genome import validate_skill, mutate, split_skill
from skill_evolver.budget import Budget, BudgetExceeded
from skill_evolver.store import Store
from skill_evolver.evaluator import evaluate

CONFIG = Path(__file__).resolve().parents[1] / 'experiments/pilot/config.json'


class FoundationTests(unittest.TestCase):
    def setUp(self):
        self.config = load_config(CONFIG)

    def test_config_and_public_boundary(self):
        self.assertNotIn('expected', public_case(self.config['splits']['search'][0]))
        for mutation in ['overlap', 'real', 'missing', 'repeat', 'path']:
            config = copy.deepcopy(self.config)
            if mutation == 'overlap':
                config['splits']['final'][0]['group_id'] = 'search-group-0'
            elif mutation == 'real':
                config['model']['provider'] = 'http'
            elif mutation == 'missing':
                del config['budgets']
            elif mutation == 'repeat':
                config['repeats']['search'] = 2
            else:
                config['splits']['search'][0]['output_paths'] = ['../escape']
            with self.assertRaises(ValueError):
                validate_config(config)

    def test_body_metadata_and_links(self):
        source = Path(self.config['skill_path'])
        original = source.read_text()
        child, operation = mutate(original, random.Random(1))
        self.assertEqual(split_skill(original)[0], split_skill(child)[0])
        validate_skill(child, original, source.parent)
        with self.assertRaises(ValueError):
            validate_skill(child.replace('uppercase-file','different'), original, source.parent)
        with self.assertRaises(ValueError):
            validate_skill(original + '\n[broken](absent.txt)', original, source.parent)
        with self.assertRaises(ValueError):
            validate_skill(original + '\n[broken](#missing)', original, source.parent)

    def test_fixed_execution_counts_require_integers(self):
        for section, field in [('search', 'elites'), ('search', 'tournament_size'),
                               ('repeats', 'search'), ('repeats', 'selection'), ('repeats', 'final'),
                               (None, 'infrastructure_retries')]:
            target = self.config[section] if section else self.config
            for invalid in (float(target[field]), True, False):
                config = copy.deepcopy(self.config)
                (config[section] if section else config)[field] = invalid
                with self.subTest(section=section, field=field, invalid=invalid):
                    with self.assertRaises(ValueError):
                        validate_config(config)

    def test_reserve_settle_and_unknown(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            budget = Budget(store, 'ga/0/search', {'tokens':10,'money':10}, 1)
            budget.reserve('a', 8)
            with self.assertRaises(BudgetExceeded):
                budget.reserve('b', 3)
            budget.settle('a', 4)
            budget.reserve('b', 6)
            budget.settle('b', None)
            self.assertEqual(budget.usage(), {'tokens':10,'money':10})
            self.assertFalse(budget.reserve('b', 6))
            other = Budget(store, 'sequential/0/search', {'tokens':10,'money':10}, 1)
            self.assertEqual(other.usage()['tokens'], 0)

    def test_torn_journal(self):
        with tempfile.TemporaryDirectory() as directory:
            store = Store(directory)
            store.append({'kind':'one'})
            with store.journal.open('ab') as handle:
                handle.write(b'{"torn":')
            self.assertEqual(len(store.events()), 1)
            store.append({'kind':'two'})
            self.assertEqual(len(store.events()), 2)

    def test_external_evaluator(self):
        case = self.config['splits']['search'][0]
        result = {'status':'success','skill_loaded':True,'outputs':case['expected']}
        self.assertEqual(evaluate(result,case,self.config['sandbox'])['score'],1)
        for status in ['task_failure','timeout']:
            result['status'] = status
            self.assertEqual(evaluate(result,case,self.config['sandbox'])['score'],0)
        result['status'] = 'infrastructure_error'
        self.assertFalse(evaluate(result,case,self.config['sandbox'])['complete'])

    def test_numeric_and_public_allowlist(self):
        for section, field in [('limits','max_steps'), ('limits','max_input_tokens'), ('limits','max_output_tokens'), ('sandbox','max_files'), ('sandbox','max_file_bytes'), ('sandbox','max_total_bytes')]:
            for invalid in [True, 1.5, float('nan'), float('inf'), -1, 0]:
                config = copy.deepcopy(self.config)
                config[section][field] = invalid
                with self.subTest(section=section, field=field, invalid=invalid):
                    with self.assertRaises(ValueError):
                        validate_config(config)
        for field in ['tokens','money']:
            config = copy.deepcopy(self.config)
            config['budgets']['search'][field] = float('nan')
            with self.assertRaises(ValueError):
                validate_config(config)
        case = dict(self.config['splits']['search'][0], hidden_answer='secret', api_key='private')
        self.assertEqual(set(public_case(case)), {'id','group_id','prompt','files','output_paths','operation'})

    def test_bundled_reference_fails_closed(self):
        source = Path(self.config['skill_path'])
        original = source.read_text()
        with tempfile.TemporaryDirectory() as directory:
            Path(directory, 'asset.txt').write_text('exists')
            with self.assertRaisesRegex(ValueError, 'staging'):
                validate_skill(original + '\n[asset](asset.txt)', original, directory)

    def test_exact_public_task_overlap(self):
        config = copy.deepcopy(self.config)
        original = config['splits']['search'][0]
        final = config['splits']['final'][0]
        for field in ['prompt','files','operation','output_paths','expected']:
            final[field] = copy.deepcopy(original[field])
        with self.assertRaisesRegex(ValueError, 'public task overlaps'):
            validate_config(config)

    def test_malformed_shapes_fail_cleanly(self):
        for key, value in [('limits', []), ('model', None), ('splits', []), ('search', None), ('pricing', {'version':'bad','per_token':float('nan')})]:
            config = copy.deepcopy(self.config)
            config[key] = value
            with self.assertRaises(ValueError):
                validate_config(config)

    def test_identifier_types_and_safe_representation(self):
        for invalid in [1, True, None, '', 'a/b', 'a\\b', '.', '..', 'a b', 'a\n', 'a:1']:
            for field in ['experiment_id','id','group_id']:
                config = copy.deepcopy(self.config)
                if field == 'experiment_id':
                    config[field] = invalid
                else:
                    config['splits']['search'][0][field] = invalid
                with self.subTest(field=field,invalid=invalid), self.assertRaises(ValueError):
                    validate_config(config)
        config = copy.deepcopy(self.config)
        config['splits']['search'][0]['id'] = 1
        config['splits']['search'][1]['id'] = '1'
        with self.assertRaises(ValueError):
            validate_config(config)
