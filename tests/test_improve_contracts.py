"""Acceptance contracts for budgets, approval drift, strict JSON, proposals and crash recovery."""
import json
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest.mock import patch

try:
    import test_improve as base
except ImportError:
    from tests import test_improve as base
from skill_evolver.cli_provider import CLIProvider, ProviderError, load_json
from skill_evolver.improve import DEFAULT_LIMITS, Calls, ImproveStore, prepare, run
from skill_evolver.improve_report import report
from skill_evolver.store import Store
from skill_evolver import improve_eval as ev, improve_report


SKILL, evaluation = base.SKILL, base.evaluation
BODY = SKILL.split('---\n', 2)[2]


def replace_content(transform):
    """Run the fake CLI normally, then substitute the parsed structured content."""
    real = CLIProvider.complete
    def complete(provider, context, schema, **kwargs):
        result = real(provider, context, schema, **kwargs)
        return {**result, 'content': transform(context, result['content'])}
    return patch.object(CLIProvider, 'complete', complete)


class Fixture(unittest.TestCase):
    setUp = base.ImprovementTests.setUp
    tearDown = base.ImprovementTests.tearDown
    calls = base.ImprovementTests.calls
    prepared = base.ImprovementTests.prepared

    def edit_manifest(self, mutate):
        path = self.output / 'manifest.json'
        manifest = json.loads(path.read_text())
        mutate(manifest)
        path.write_text(json.dumps(manifest))

    def journal(self, kind):
        return [e for e in Store(self.output).events() if e['kind'] == kind]

    def assertNoAdoptableResult(self):
        self.assertFalse((self.output / 'candidate' / 'SKILL.md').exists())
        self.assertEqual(self.source.read_text(), SKILL)

    def fresh(self):
        if self.output.exists():
            shutil.rmtree(self.output)
        if self.log.exists():
            self.log.unlink()
        return self.prepared()


class ApprovalAndProfileContracts(Fixture):
    def test_changed_limits_model_or_identity_invalidate_approval(self):
        mutations = {
            'max_iterations': lambda m: m['limits'].update(max_iterations=2),
            'max_calls': lambda m: m['limits'].update(max_calls=50),
            'requested_model': lambda m: m['provider_profile'].update(model='claude-test-2'),
            'reported_model': lambda m: m.update(reported_model='claude-other-1'),
            'model_identity': lambda m: m.update(model_identity='requested_only'),
        }
        for name, mutate in mutations.items():
            with self.subTest(changed=name):
                prepared = self.fresh()
                self.edit_manifest(mutate)
                with self.assertRaises(ValueError):
                    run(self.output, prepared['approval_hash'])
                self.assertEqual(len(self.calls()), 1)
                self.assertNotEqual(report(self.output)['approval_hash'], prepared['approval_hash'])
                self.assertNoAdoptableResult()

    def test_reapproved_profile_drift_is_rejected_before_any_model_call(self):
        prepared = self.prepared()
        self.edit_manifest(lambda m: m['provider_profile'].update(profile_version=0))
        reapproved = report(self.output)['approval_hash']
        self.assertNotEqual(reapproved, prepared['approval_hash'])
        with self.assertRaises(ValueError):
            run(self.output, reapproved)
        self.assertEqual(len(self.calls()), 1)
        self.assertNoAdoptableResult()

    def test_cli_upgrade_between_prepare_and_run_makes_no_model_call(self):
        prepared = self.prepared()
        (self.bin / 'claude').write_text(base.FAKE.replace('Claude Code 2.1.310', 'Claude Code 2.1.311'))
        with self.assertRaises(ValueError):
            run(self.output, prepared['approval_hash'])
        self.assertEqual(len(self.calls()), 1)
        self.assertNoAdoptableResult()

    def test_cli_upgrade_during_run_stops_without_further_model_calls(self):
        prepared = self.prepared()
        def upgrade_after_first(context, content):
            (self.bin / 'claude').write_text(base.FAKE.replace('Claude Code 2.1.310', 'Claude Code 2.1.311'))
            return content
        with replace_content(upgrade_after_first):
            result = run(self.output, prepared['approval_hash'])
        self.assertEqual((result['status'], result['reason']), ('incomplete', 'provider_profile_changed'))
        self.assertFalse(result['complete'])
        self.assertEqual(len(self.calls()), 2)
        self.assertNoAdoptableResult()


class _CountingProvider:
    def __init__(self):
        self.invocations = 0

    def complete(self, context, schema, **kwargs):
        self.invocations += 1
        return {'content': {'output': 'x'}, 'reported_model': None, 'model_identity': 'requested_only',
                'status': 'success', 'observed_usage': None}


class BudgetContracts(Fixture):
    def calls_with(self, **limits):
        store = Store(Path(self.temporary.name) / 'unit')
        provider = _CountingProvider()
        return store, provider, dict(DEFAULT_LIMITS, **limits)

    def test_call_limit_prevents_provider_invocation(self):
        store, provider, limits = self.calls_with(max_calls=2)
        for identity in ('a/1', 'a/2'):
            store.append({'kind': 'call_started', 'call_id': identity, 'phase': 'a'})
        with self.assertRaises(ProviderError) as raised:
            Calls(store, provider, limits).complete('a/3', {'task': 'x'}, ev.OUTPUT_SCHEMA)
        self.assertEqual(raised.exception.reason, 'call_limit')
        self.assertEqual(provider.invocations, 0)
        self.assertNotIn('a/3', {e['call_id'] for e in store.events()})

    def test_expired_deadline_prevents_provider_invocation(self):
        store, provider, limits = self.calls_with(run_deadline=1)
        now = [100.0]
        with patch('skill_evolver.improve.time') as clock:
            clock.monotonic.side_effect = lambda: now[0]
            calls = Calls(store, provider, limits)
            now[0] += 2
            with self.assertRaises(ProviderError) as raised:
                calls.complete('a/1', {'task': 'x'}, ev.OUTPUT_SCHEMA)
        self.assertEqual(raised.exception.reason, 'run_deadline')
        self.assertEqual(provider.invocations, 0)
        self.assertEqual(store.events(), [])

    def test_run_stops_at_global_call_cap_including_prepare(self):
        self.prepared()
        self.edit_manifest(lambda m: m['limits'].update(max_calls=5))
        result = run(self.output, report(self.output)['approval_hash'])
        self.assertEqual((result['status'], result['reason']), ('incomplete', 'call_limit'))
        self.assertEqual(len(self.calls()), 5)
        self.assertNoAdoptableResult()


class StrictJudgmentContracts(unittest.TestCase):
    def judgment(self):
        return {'criteria': [{'id': 'purpose', 'passed': True, 'reason': 'ok'},
                             {'id': 'quality', 'passed': False, 'reason': 'vague'},
                             {'id': 'style', 'passed': True, 'reason': 'concise'}]}

    def test_judgment_rows_must_match_fixed_criteria_exactly(self):
        ev.validate_response(self.judgment(), 'judgment', evaluation())
        mutations = {
            'missing': lambda j: j['criteria'].pop(),
            'duplicate': lambda j: j['criteria'].append(dict(j['criteria'][0])),
            'unknown': lambda j: j['criteria'].append({'id': 'extra', 'passed': True, 'reason': 'x'}),
            'row_extra_key': lambda j: j['criteria'][0].update(score=1),
            'row_missing_key': lambda j: j['criteria'][0].pop('reason'),
            'top_extra_key': lambda j: j.update(summary='x'),
            'int_passed': lambda j: j['criteria'][0].update(passed=1),
            'string_passed': lambda j: j['criteria'][0].update(passed='true'),
            'empty_reason': lambda j: j['criteria'][0].update(reason='  '),
            'not_list': lambda j: j.update(criteria={'purpose': True}),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                value = self.judgment(); mutate(value)
                with self.assertRaises(ValueError):
                    ev.validate_response(value, 'judgment', evaluation())

    def test_evaluation_schema_violations_are_rejected(self):
        mutations = {
            'duplicate_criterion': lambda e: e['criteria'].append(dict(e['criteria'][0])),
            'empty_rubric': lambda e: e.update(criteria=[]),
            'missing_key': lambda e: e.pop('constraints'),
            'criterion_extra_key': lambda e: e['criteria'][0].update(weight=2),
            'case_extra_key': lambda e: e['development'][0].update(answer='secret'),
            'duplicate_case_id': lambda e: e['heldout'][0].update(id='dev0'),
            'two_cases': lambda e: e['development'].pop(),
            'oversized': lambda e: e['development'][0].update(prompt='x' * (ev.MAX_TEXT + 1)),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                value = evaluation(); mutate(value)
                with self.assertRaises(ValueError):
                    ev.validate_evaluation(value)

    def test_non_finite_and_duplicate_key_json_is_rejected(self):
        for raw in ('{"a": NaN}', '{"a": Infinity}', '{"a": -Infinity}',
                    '{"a": 1, "a": 2}', '{"criteria": [{"id": "x", "id": "y"}]}'):
            with self.subTest(raw=raw), self.assertRaises(ValueError):
                load_json(raw)

    def test_codex_structured_text_with_duplicate_keys_or_nan_is_rejected(self):
        provider = CLIProvider('codex', 'gpt-test-1')
        for text in ('{"output": "a", "output": "b"}', '{"output": NaN}'):
            events = [{'type': 'thread.started'},
                      {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': text}},
                      {'type': 'turn.completed', 'usage': {}}]
            with self.subTest(text=text), self.assertRaises(ValueError):
                provider._parse(events)


class SavedJsonContracts(Fixture):
    def test_saved_evaluation_with_duplicate_key_or_nan_is_not_runnable(self):
        good = json.dumps(evaluation())
        for name, raw in [('duplicate', good[:-1] + ', "purpose": "Another purpose"}'),
                          ('nan', good.replace('"required": true', '"required": NaN', 1))]:
            with self.subTest(case=name):
                prepared = self.fresh()
                (self.output / 'evaluation.json').write_text(raw)
                with self.assertRaises(ValueError): report(self.output)
                with self.assertRaises(ValueError): run(self.output, prepared['approval_hash'])
                self.assertEqual(len(self.calls()), 1)

    def test_malformed_judgment_during_run_is_incomplete_not_zero_score(self):
        def judge(mutate):
            def transform(context, content):
                if 'criteria' in context and 'output' in context:
                    mutate(content)
                return content
            return transform
        mutations = {
            'missing': lambda c: c['criteria'].pop(),
            'duplicate': lambda c: c['criteria'].append(dict(c['criteria'][0])),
            'unknown': lambda c: c['criteria'].append({'id': 'extra', 'passed': True, 'reason': 'x'}),
            'int_passed': lambda c: c['criteria'][0].update(passed=0),
        }
        for name, mutate in mutations.items():
            with self.subTest(case=name):
                prepared = self.fresh()
                with replace_content(judge(mutate)):
                    result = run(self.output, prepared['approval_hash'])
                self.assertEqual(result['status'], 'incomplete')
                self.assertFalse(result['complete'])
                self.assertEqual(len(self.calls()), 3)  # prepare, first execute, first judge
                self.assertEqual(self.journal('trial'), [])
                self.assertNoAdoptableResult()


class ProposalIterationContracts(Fixture):
    def proposals(self):
        return [c for c in self.calls() if 'current_skill' in c]

    def purpose_checks(self):
        return [c for c in self.calls() if 'proposed_skill' in c]

    def test_each_invalid_proposal_consumes_one_of_three_iterations(self):
        invalid = {
            'frontmatter_changed': SKILL.replace('Write clear responses', 'Write anything'),
            'frontmatter_missing': BODY,
            'companion_reference': SKILL + '\nRun scripts/check.sh first.\n',
            'relative_link': SKILL + '\nSee [guide](guide.md).\n',
            'unclosed_fence': SKILL + '\n```\ncode\n',
            'over_skill_size_limit': SKILL + 'x' * (64 * 1024) + '\n',
            'over_response_limit': SKILL + 'x' * (ev.MAX_TEXT + 1) + '\n',
            'empty_text': '',
        }
        for name, text in invalid.items():
            with self.subTest(case=name):
                prepared = self.fresh()
                with replace_content(lambda c, content: {'skill_text': text} if 'current_skill' in c else content):
                    result = run(self.output, prepared['approval_hash'])
                self.assertEqual(result['status'], 'no_observed_improvement')
                self.assertTrue(result['complete'])
                self.assertEqual(len(self.proposals()), 3)
                self.assertEqual(len(self.purpose_checks()), 0)
                # prepare + 6 baseline development + 3 proposals + 6 original heldout
                self.assertEqual(len(self.calls()), 16)
                self.assertEqual({e['iteration'] for e in self.journal('candidate_rejected')}, {1, 2, 3})
                self.assertEqual((self.output / 'candidate' / 'SKILL.md').read_text(), SKILL)
                self.assertEqual(self.source.read_text(), SKILL)

    def test_invalid_first_proposal_does_not_grant_a_fourth(self):
        prepared = self.prepared()
        seen = []
        def first_invalid(context, content):
            if 'current_skill' in context:
                seen.append(1)
                if len(seen) == 1:
                    return {'skill_text': BODY}
            return content
        with replace_content(first_invalid):
            result = run(self.output, prepared['approval_hash'])
        self.assertEqual(result['status'], 'observed_improvement')
        self.assertEqual(len(self.proposals()), 3)
        self.assertEqual(len(self.purpose_checks()), 2)


class RecoveryContracts(Fixture):
    def test_failed_call_finished_persistence_is_not_reissued(self):
        prepared = self.prepared()
        target = 'development/original/dev1/execute'
        real = ImproveStore.append
        def append(store, event):
            if event.get('kind') == 'call_finished' and event.get('call_id') == target:
                raise OSError('disk full')
            return real(store, event)
        with patch.object(ImproveStore, 'append', append):
            try:
                run(self.output, prepared['approval_hash'])
            except OSError:
                pass  # either recorded as incomplete or re-raised; neither may retry
        count = len(self.calls())
        self.assertEqual(count, 4)  # prepare, dev0 execute/judge, dev1 execute
        rerun = run(self.output, prepared['approval_hash'])
        self.assertEqual(len(self.calls()), count)
        self.assertEqual(rerun['status'], 'incomplete')
        self.assertIn(target, rerun['unresolved_calls'])
        self.assertNoAdoptableResult()

    def test_report_failure_after_completion_does_not_rerun_calls(self):
        prepared = self.prepared()
        real = improve_report.write_text
        def write_text(store, name, value):
            if name == 'comparison.md':
                raise OSError('disk full')
            return real(store, name, value)
        with patch('skill_evolver.improve_report.write_text', write_text):
            with self.assertRaises(OSError):
                run(self.output, prepared['approval_hash'])
        count = len(self.calls())
        self.assertEqual(count, 43)
        rerun = run(self.output, prepared['approval_hash'])
        self.assertEqual(rerun['status'], 'observed_improvement')
        self.assertEqual(len(self.calls()), count)

    def test_lock_contention_fails_without_model_calls(self):
        prepared = self.prepared()
        with Store(self.output).lock():
            with self.assertRaises(ValueError):
                run(self.output, prepared['approval_hash'])
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(json.loads((self.output / 'manifest.json').read_text())['state'], 'draft')


class PrepareScopeContracts(Fixture):
    def test_out_of_scope_skills_are_rejected_before_model_calls(self):
        cases = {
            'missing_frontmatter': BODY,
            'missing_description': SKILL.replace('description: Write clear responses\n', ''),
            'companion_reference': SKILL + '\nRead references/style.md.\n',
            'relative_link': SKILL + '\nSee [guide](guide.md).\n',
            'over_64_kib': SKILL + 'x' * (64 * 1024) + '\n',
        }
        for name, text in cases.items():
            with self.subTest(case=name):
                self.source.write_text(text)
                with self.assertRaises(ValueError):
                    self.prepared()
                self.assertEqual(self.calls(), [])
                self.assertFalse(self.output.exists())


if __name__ == '__main__': unittest.main()
