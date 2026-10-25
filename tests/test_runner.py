import copy
import json
import os
import unittest
from unittest.mock import patch

from skill_evolver.sandbox import SandboxError
from skill_evolver.model import ModelError
from skill_evolver.runner import TrialRunner, reservation_tokens


def config():
    return {'sandbox': {'image': 'alpine:3', 'timeout_seconds': 5, 'max_file_bytes': 4096,
                        'max_total_bytes': 16384, 'max_files': 10},
            'model': {'provider': 'mock', 'id': 'deterministic-v1', 'options': {}},
            'limits': {'max_steps': 4, 'max_input_tokens': 10000,
                       'max_output_tokens': 1000, 'timeout_seconds': 15}}


def case():
    return {'id': 'x', 'group_id': 'g', 'prompt': 'Convert input.txt to uppercase result.txt',
            'files': {'input.txt': 'Hello\n'}, 'operation': 'uppercase',
            'output_paths': ['result.txt'], 'expected': {'result.txt': 'HIDDEN-ANSWER'}}


class RunnerTests(unittest.TestCase):
    def test_expected_never_touched(self):
        class PrivateAnswer:
            def __deepcopy__(self, memo):
                raise AssertionError('private expected was accessed')
        public = case()
        public['expected'] = PrivateAnswer()
        c = config()
        c['limits']['max_input_tokens'] = 1
        result = TrialRunner(c).run('skill', public, trial_id='test')
        self.assertEqual(result['termination_reason'], 'model input envelope exceeded')

    def test_prepare_pins_once_and_resume_verifies_exact_image(self):
        image = 'sha256:' + 'a' * 64
        with patch('skill_evolver.runner.resolve_image', return_value=image) as resolve:
            runner = TrialRunner(config())
            self.assertEqual(runner.prepare_environment(), image)
            self.assertEqual(runner.prepare_environment(), image)
            resolve.assert_called_once_with('alpine:3')
            self.assertEqual(runner.config['sandbox']['image'], image)
            with self.assertRaises(SandboxError):
                runner.prepare_environment('sha256:' + 'b' * 64)
        with patch('skill_evolver.runner.resolve_image', return_value=image) as resolve:
            runner = TrialRunner(config())
            self.assertEqual(runner.prepare_environment(image), image)
            resolve.assert_called_once_with(image)
        with patch('skill_evolver.runner.resolve_image', side_effect=SandboxError('missing pinned image')):
            with self.assertRaises(SandboxError):
                TrialRunner(config()).prepare_environment(image)

    def test_reservation(self):
        self.assertEqual(reservation_tokens(config()), 44000)

    def test_reject_provider(self):
        c = config()
        c['model']['provider'] = 'http'
        with self.assertRaises(ValueError):
            TrialRunner(c)

    def test_direct_unsafe_inputs_never_start_docker(self):
        for path in ('../answer', '/host/answer'):
            public = case()
            public['files'] = {path: 'text'}
            with patch('skill_evolver.runner.DockerSandbox') as sandbox:
                result = TrialRunner(config()).run('skill', public, trial_id='test')
                self.assertEqual(result['status'], 'task_failure')
                sandbox.assert_not_called()

    def test_input_envelope_before_docker(self):
        c = config()
        c['limits']['max_input_tokens'] = 5
        with patch('skill_evolver.runner.DockerSandbox') as sandbox:
            result = TrialRunner(c).run('skill', case(), trial_id='test')
            self.assertEqual(result['status'], 'task_failure')
            sandbox.assert_not_called()


class OpenAIRunnerTests(unittest.TestCase):
    def setUp(self):
        patcher = patch('skill_evolver.runner.resolve_image', return_value='sha256:' + 'a' * 64)
        patcher.start()
        self.addCleanup(patcher.stop)

    def setup_runner(self):
        c = config()
        c['model'] = {'provider': 'openai', 'id': 'configured-model', 'options': {},
                      'api_key_env': 'TEST_KEY', 'count_fee_acknowledged': True}
        return TrialRunner(c)

    def test_unknown_and_known_usage(self):
        for tokens, status in ((None, 'indeterminate'), (8, 'task_failure')):
            runner = self.setup_runner()
            with patch('skill_evolver.runner.DockerSandbox') as cls:
                sandbox = cls.return_value
                sandbox.deadline = __import__('time').monotonic() + 10
                sandbox.environment_hash = 'hash'
                with patch.object(runner.model, 'complete', side_effect=ModelError('failed', tokens=tokens)):
                    result = runner.run('skill', case(), trial_id='real')
                self.assertEqual(result['tokens'], tokens)
                self.assertEqual(result['status'], status)
                sandbox.tool.assert_not_called()
                sandbox.stop.assert_called_once()
                sandbox.close.assert_called_once()

    def test_unknown_broker_usage_survives_stop_failure(self):
        self.assert_secondary_failure_preserves_unknown('stop')

    def test_unknown_broker_usage_survives_cleanup_failure(self):
        self.assert_secondary_failure_preserves_unknown('cleanup')

    def assert_secondary_failure_preserves_unknown(self, stage):
        for failure in (SandboxError('secondary failure'), OSError('secondary failure'),
                        ValueError('secondary failure'), TimeoutError('secondary failure')):
            with self.subTest(stage=stage, failure=type(failure).__name__):
                runner = self.setup_runner()
                with patch('skill_evolver.runner.DockerSandbox') as cls:
                    sandbox = cls.return_value
                    sandbox.deadline = __import__('time').monotonic() + 10
                    sandbox.environment_hash = 'hash'
                    getattr(sandbox, 'stop' if stage == 'stop' else 'close').side_effect = failure
                    with patch.object(runner.model, 'complete', side_effect=ModelError('unknown transport outcome', tokens=None)):
                        result = runner.run('skill', case(), trial_id='unknown-secondary')
                    self.assertIsNone(result['tokens'])
                    self.assertEqual(result['status'], 'indeterminate')
                    self.assertEqual(result['termination_reason'], 'unknown transport outcome')
                    self.assertEqual(result['outputs'], {})
                    self.assertEqual(result['secondary_errors'], [{'stage': stage, 'reason': 'secondary failure'}])
                    sandbox.close.assert_called_once()
                    sandbox.collect.assert_not_called()

    def test_known_usage_stop_failure_remains_infrastructure_error(self):
        runner = self.setup_runner()
        with patch('skill_evolver.runner.DockerSandbox') as cls:
            sandbox = cls.return_value
            sandbox.deadline = __import__('time').monotonic() + 10
            sandbox.environment_hash = 'hash'
            sandbox.stop.side_effect = SandboxError('known stop failure')
            with patch.object(runner.model, 'complete', side_effect=ModelError('known model failure', tokens=8)):
                result = runner.run('skill', case(), trial_id='known-secondary')
            self.assertEqual(result['tokens'], 8)
            self.assertEqual(result['status'], 'infrastructure_error')
            self.assertEqual(result['termination_reason'], 'known stop failure')

    def test_schema_before_tools_and_known_cost_retained(self):
        runner = self.setup_runner()
        with patch('skill_evolver.runner.DockerSandbox') as cls:
            sandbox = cls.return_value
            sandbox.deadline = __import__('time').monotonic() + 10
            sandbox.environment_hash = 'hash'
            with patch.object(runner.model, 'complete', return_value={'content': {'tool': 'host_shell', 'arguments': {}}, 'tokens': 10}):
                result = runner.run('skill', case(), trial_id='real')
            self.assertEqual(result['tokens'], 10)
            self.assertEqual(result['status'], 'task_failure')
            sandbox.tool.assert_not_called()

    def test_actual_token_aggregation_public_context_and_read_evidence(self):
        runner = self.setup_runner()
        with patch('skill_evolver.runner.DockerSandbox') as cls:
            sandbox = cls.return_value
            sandbox.deadline = __import__('time').monotonic() + 10
            sandbox.environment_hash = 'hash'
            sandbox.tool.return_value = 'skill'
            sandbox.collect.return_value = {'result.txt': 'HELLO\n'}
            completions = [{'content': {'tool': 'read_file', 'arguments': {'path': '/SKILL.md'}}, 'tokens': 10},
                           {'content': {'finish': True}, 'tokens': 12}]
            with patch.object(runner.model, 'complete', side_effect=completions) as complete:
                result = runner.run('skill', case(), trial_id='real')
            self.assertEqual(result['tokens'], 22)
            self.assertEqual(result['status'], 'success')
            self.assertTrue(result['skill_loaded'])
            for call in complete.call_args_list:
                self.assertNotIn('HIDDEN-ANSWER', json.dumps(call.args[0]))
                self.assertEqual(call.kwargs['max_input_tokens'], 10000)
                self.assertEqual(call.kwargs['max_output_tokens'], 1000)


@unittest.skipUnless(os.environ.get('SKILL_EVOLVER_DOCKER_TESTS') == '1', 'Docker integration opt-in')
class RunnerDockerTests(unittest.TestCase):
    def test_actual_conversion_no_expected_and_fresh_conversation(self):
        runner = TrialRunner(config())
        before = copy.deepcopy(case())
        first = runner.run('Read input and convert it.', before, trial_id='one')
        self.assertEqual(first['status'], 'success', first)
        self.assertEqual(first['outputs'], {'result.txt': 'HELLO\n'})
        self.assertTrue(first['skill_loaded'])
        self.assertNotIn('HIDDEN-ANSWER', json.dumps(first))
        self.assertLessEqual(first['tokens'], reservation_tokens(config()))
        self.assertEqual(before, case())
        second = runner.run('Read input and convert it.', case(), trial_id='two')
        self.assertEqual(first['transcript'], second['transcript'])
        self.assertEqual(first['environment_hash'], second['environment_hash'])
        first['outputs']['result.txt'] = 'mutated host copy'
        self.assertEqual(second['outputs']['result.txt'], 'HELLO\n')

    def test_copy_and_step_limit(self):
        public = case()
        public['operation'] = 'copy'
        result = TrialRunner(config()).run('Copy input.', public, trial_id='copy')
        self.assertEqual(result['outputs'], {'result.txt': 'Hello\n'})
        c = config()
        c['limits']['max_steps'] = 1
        self.assertEqual(TrialRunner(c).run('skill', case(), trial_id='short')['termination_reason'], 'max_steps')
