import io
import json
import os
from pathlib import Path
import shutil
import signal
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

from skill_evolver.cli import main
from skill_evolver.cli_provider import CLIProvider, ProviderError, bounded_process, login_environment
from skill_evolver.improve import ImproveStore, prepare, run, load_bundle, fingerprint
from skill_evolver.improve_report import report
from skill_evolver import improve_eval as ev


SKILL = '---\nname: clear-writing\ndescription: Write clear responses\n---\n\nWrite a clear response to the request.\n'


def evaluation():
    return {'purpose': 'Write a clear response', 'constraints': ['Keep the writing purpose'],
            'execution_scope': 'text_only', 'unsupported_reason': '',
            'development': [{'id': 'dev' + str(i), 'group_id': 'devg' + str(i), 'prompt': 'Development ' + str(i)} for i in range(3)],
            'heldout': [{'id': 'held' + str(i), 'group_id': 'heldg' + str(i), 'prompt': 'Heldout ' + str(i)} for i in range(3)],
            'criteria': [{'id': 'purpose', 'description': 'Answer the request', 'required': True},
                         {'id': 'quality', 'description': 'Be clear', 'required': False}],
            'aggregation': 'binary_pass_count'}


FAKE = r'''#!/usr/bin/env python3
import json, os, sys, time
from pathlib import Path
provider = Path(sys.argv[0]).name
args = sys.argv[1:]
if '--version' in args:
    print('Claude Code 2.1.310' if provider == 'claude' else 'codex-cli 0.164.0'); sys.exit()
if '--help' in args:
    print('--safe-mode --restricted --tools --disallowedTools --strict-mcp-config --json-schema --output-format --no-session-persistence --ignore-user-config --ignore-rules --ephemeral --output-schema --json --sandbox'); sys.exit()
if args[:2] == ['auth','status']:
    print(json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'})); sys.exit()
if args[:2] == ['login','status']:
    print('Logged in using ChatGPT', file=sys.stderr); sys.exit()
assert not os.environ.get('OPENAI_API_KEY') and not os.environ.get('ANTHROPIC_API_KEY')
assert not os.environ.get('CLAUDE_CODE_OAUTH_TOKEN')
if provider == 'claude':
    assert '--bare' not in args and '--safe-mode' in args and '--restricted' in args
    assert args[args.index('--tools')+1] == ''
    assert '--strict-mcp-config' in args
else:
    assert '--ignore-user-config' in args and '--ignore-rules' in args and '--ephemeral' in args
    assert args[args.index('--sandbox')+1] == 'read-only'
    assert 'features.shell_tool=false' in args and 'web_search="disabled"' in args
context = json.load(sys.stdin)
with open(os.environ['FAKE_LOG'], 'a') as out:
    out.write(json.dumps(context)+'\n')
mode = os.environ.get('FAKE_MODE','')
if mode == 'timeout': time.sleep(10)
if mode == 'flood': print('x'*100000); sys.exit()
if mode == 'badjson': print('not json'); sys.exit()
if mode == 'error': print('rate limit reached', file=sys.stderr); sys.exit(1)
if 'skill' in context and 'Prepare an evaluation' in context['task']:
    result = json.loads(os.environ['FAKE_EVAL'])
elif 'current_skill' in context:
    result = {'skill_text': context['current_skill']+'\nIMPROVED\n'}
elif 'proposed_skill' in context:
    result = {'preserved': mode != 'purpose', 'reason': 'The writing purpose is unchanged'}
elif 'output' in context:
    result = {'criteria': [{'id': c['id'], 'passed': c['id']=='purpose' or context['output']=='better', 'reason': 'Fixed criterion checked'} for c in context['criteria']]}
else:
    result = {'output': 'better' if 'IMPROVED' in context.get('skill','') else 'base'}
if provider == 'claude':
    print(json.dumps({'type':'system','subtype':'init','tools':[],'mcp_servers':[]}))
    if mode == 'tools': print(json.dumps({'type':'assistant','message':{'content':[{'type':'tool_use','name':'Bash'}]}}))
    print(json.dumps({'type':'result','subtype':'success','is_error':False,'structured_output':result,'usage':{'input_tokens':10,'output_tokens':5},'modelUsage':({} if os.environ.get('FAKE_NO_MODEL') else {os.environ.get('FAKE_REPORTED_MODEL','claude-test-1'):{}})}))
else:
    print(json.dumps({'type':'thread.started','thread_id':'fake'}))
    if mode == 'tools': print(json.dumps({'type':'item.completed','item':{'type':'command_execution','command':'touch bad'}}))
    print(json.dumps({'type':'item.completed','item':{'type':'agent_message','text':json.dumps(result)}}))
    print(json.dumps({'type':'turn.completed','usage':{'input_tokens':10,'output_tokens':5}}))
'''


class ImprovementTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        for name in ('claude', 'codex'):
            executable = self.bin / name; executable.write_text(FAKE); executable.chmod(0o755)
        self.source = self.root / 'skill' / 'SKILL.md'
        self.source.parent.mkdir(); self.source.write_text(SKILL)
        self.output = self.root / 'run'
        self.log = self.root / 'calls.jsonl'
        self.environment = patch.dict(os.environ, {'PATH': str(self.bin) + os.pathsep + os.environ['PATH'],
            'FAKE_LOG': str(self.log), 'FAKE_EVAL': json.dumps(evaluation()), 'FAKE_MODE': '',
            'OPENAI_API_KEY': 'must-not-pass', 'ANTHROPIC_API_KEY': 'must-not-pass',
            'CLAUDE_CODE_OAUTH_TOKEN': 'must-not-pass'})
        self.environment.start()

    def tearDown(self):
        self.environment.stop(); self.temporary.cleanup()

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def prepared(self, provider='claude'):
        return prepare(self.source, provider, 'claude-test-1' if provider == 'claude' else 'gpt-test-1', self.output)

    def test_both_cli_profiles_and_unexpected_tools(self):
        for name, model in [('claude', 'claude-test-1'), ('codex', 'gpt-test-1')]:
            provider = CLIProvider(name, model)
            provider.inspect()
            result = provider.complete({'task':'text only'}, ev.OUTPUT_SCHEMA)
            self.assertEqual(result['content'], {'output':'base'})
            with patch.dict(os.environ, {'FAKE_MODE':'tools'}):
                with self.assertRaisesRegex(ProviderError, 'unexpected_tool_activity'):
                    provider.complete({'task':'text only'}, ev.OUTPUT_SCHEMA)

    def test_prepare_review_and_full_comparison_are_separate(self):
        prepared = self.prepared()
        self.assertEqual(prepared['state'], 'draft')
        self.assertEqual(len(self.calls()), 1)
        self.assertEqual(self.source.read_text(), SKILL)
        before = len(self.calls())
        with self.assertRaises(ValueError): run(self.output, '0'*64)
        self.assertEqual(len(self.calls()), before)
        result = run(self.output, prepared['approval_hash'])
        self.assertEqual(result['status'], 'observed_improvement')
        self.assertEqual(len(self.calls()), 43)
        self.assertIn('IMPROVED', (self.output/'candidate/SKILL.md').read_text())
        self.assertEqual(self.source.read_text(), SKILL)
        repeated = run(self.output, prepared['approval_hash'])
        self.assertEqual(repeated['status'], 'observed_improvement')
        self.assertEqual(len(self.calls()), 43)
        self.assertEqual(result['cost_usd'], None)
        for call in self.calls():
            if 'current_skill' in call:
                self.assertNotIn('heldout', json.dumps(call).lower())
            if 'skill' in call and 'prompt' in call:
                self.assertNotIn('criteria', call)
            if 'criteria' in call:
                self.assertNotIn('skill', call)
                self.assertNotIn('current_skill', call)

    def test_codex_end_to_end_with_requested_only_model(self):
        prepared = self.prepared('codex')
        self.assertEqual(prepared['model_identity'], 'requested_only')
        result = run(self.output, prepared['approval_hash'])
        self.assertTrue(result['complete'])
        self.assertEqual(result['status'], 'observed_improvement')

    def test_canonical_json_edits_invalidate_hash_but_markdown_does_not(self):
        prepared = self.prepared()
        (self.output/'evaluation.md').write_text('Edited display only')
        self.assertEqual(report(self.output)['approval_hash'], prepared['approval_hash'])
        self.assertIn('editable source', (self.output/'evaluation.md').read_text())
        data = evaluation(); data['purpose'] = 'Another reviewed writing purpose'
        (self.output/'evaluation.json').write_text(json.dumps(data))
        with self.assertRaises(ValueError): run(self.output, prepared['approval_hash'])
        self.assertNotEqual(report(self.output)['approval_hash'], prepared['approval_hash'])
        self.assertEqual(len(self.calls()), 1)

    def test_run_uses_snapshot_despite_external_edit(self):
        prepared = self.prepared()
        real = CLIProvider.complete
        changed = False
        def edit(provider, context, schema, **kwargs):
            nonlocal changed
            if not changed:
                changed = True
                data = evaluation(); data['purpose'] = 'EXTERNAL EDIT'
                (self.output/'evaluation.json').write_text(json.dumps(data))
            return real(provider, context, schema, **kwargs)
        with patch.object(CLIProvider, 'complete', edit):
            result = run(self.output, prepared['approval_hash'])
        self.assertEqual(result['status'], 'observed_improvement')
        for call in self.calls()[1:]: self.assertNotIn('EXTERNAL EDIT', json.dumps(call))
        self.assertTrue(result['warnings'])
        with self.assertRaises(ValueError): run(self.output, result['approval_hash'])

    def test_purpose_rejection_preserves_original(self):
        prepared = self.prepared()
        with patch.dict(os.environ, {'FAKE_MODE':'purpose'}):
            result = run(self.output, prepared['approval_hash'])
        self.assertEqual(result['status'], 'no_observed_improvement')
        self.assertEqual((self.output/'candidate/SKILL.md').read_text(), SKILL)
        self.assertEqual(len(self.calls()), 19)  # prepare, baseline, 3 proposal/purpose, original heldout

    def test_provider_failure_keeps_original_and_never_retries(self):
        prepared = self.prepared()
        with patch.dict(os.environ, {'FAKE_MODE':'error'}):
            result = run(self.output, prepared['approval_hash'])
        count = len(self.calls())
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['reason'], 'rate_limit')
        self.assertEqual(self.source.read_text(), SKILL)
        rerun = run(self.output, prepared['approval_hash'])
        self.assertEqual(len(self.calls()), count)
        self.assertEqual(rerun['reason'], 'rate_limit')
        self.assertIsNone(result['observed_usage'][-1])

    def test_uncommitted_call_is_not_reissued(self):
        prepared = self.prepared()
        with (self.output/'journal.jsonl').open('a') as handle:
            handle.write(json.dumps({'kind':'call_started','call_id':'development/original/dev0/execute'})+'\n')
        result = run(self.output, prepared['approval_hash'])
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(len(self.calls()), 1)
        self.assertTrue(result['unresolved_calls'])

    def assertUnexpectedIncomplete(self, secret=None):
        manifest = json.loads((self.output/'manifest.json').read_text())
        self.assertEqual((manifest['state'], manifest['reason']), ('incomplete', 'unexpected_error'))
        self.assertFalse(report(self.output)['complete'])
        self.assertEqual(self.source.read_text(), SKILL)
        if secret:
            for path in self.output.rglob('*'):
                if path.is_file(): self.assertNotIn(secret, path.read_text())

    def test_unexpected_prepare_errors_leave_incomplete_state(self):
        for target in ('candidate', 'ev.validate_evaluation'):
            with self.subTest(target=target):
                with patch('skill_evolver.improve.' + target, side_effect=TypeError('secret-token')):
                    with self.assertRaises(TypeError): self.prepared()
                self.assertUnexpectedIncomplete('secret-token')
                shutil.rmtree(self.output)

    def test_prepare_interrupt_is_reraised_as_incomplete(self):
        with patch('skill_evolver.improve.ev.validate_evaluation', side_effect=KeyboardInterrupt):
            with self.assertRaises(KeyboardInterrupt): self.prepared()
        self.assertEqual(json.loads((self.output/'manifest.json').read_text())['reason'], 'interrupted')

    def test_unexpected_run_error_is_recorded_reraised_and_not_reissued(self):
        prepared = self.prepared()
        real = CLIProvider.complete
        def malformed(provider, context, schema, **kwargs):
            result = real(provider, context, schema, **kwargs)
            del result['reported_model']
            return result
        with patch.object(CLIProvider, 'complete', malformed):
            with self.assertRaises(KeyError): run(self.output, prepared['approval_hash'])
        self.assertUnexpectedIncomplete()
        self.assertFalse(json.loads((self.output/'comparison.json').read_text())['complete'])
        self.assertFalse((self.output/'candidate/SKILL.md').exists())
        count = len(self.calls())
        self.assertEqual(run(self.output, prepared['approval_hash'])['status'], 'incomplete')
        self.assertEqual(len(self.calls()), count)

    def test_unexpected_error_after_running_state_is_recorded(self):
        prepared = self.prepared()
        with patch('skill_evolver.improve._save_candidate', side_effect=RuntimeError('secret-token')):
            with self.assertRaises(RuntimeError): run(self.output, prepared['approval_hash'])
        self.assertUnexpectedIncomplete('secret-token')
        self.assertEqual(len(self.calls()), 1)

    def test_run_interrupt_is_reported_incomplete(self):
        prepared = self.prepared()
        with patch.object(CLIProvider, 'complete', side_effect=KeyboardInterrupt):
            result = run(self.output, prepared['approval_hash'])
        self.assertEqual((result['status'], result['reason']), ('incomplete', 'interrupted'))
        self.assertEqual(self.source.read_text(), SKILL)

    def test_unpersisted_failure_state_is_not_hidden(self):
        prepared = self.prepared()
        real = ImproveStore.write
        def failing(store, name, value):
            if name == 'manifest.json' and value.get('state') == 'incomplete':
                raise OSError('disk full')
            return real(store, name, value)
        with patch('skill_evolver.improve._save_candidate', side_effect=RuntimeError('boom')), \
             patch.object(ImproveStore, 'write', failing):
            with self.assertRaises(OSError) as raised: run(self.output, prepared['approval_hash'])
        self.assertIsInstance(raised.exception.__context__, RuntimeError)
        self.assertFalse(report(self.output)['complete'])

    def link(self, kind, name):
        target = self.output/name
        target.unlink(missing_ok=True)
        os.link(self.source, target) if kind == 'hard' else target.symlink_to(self.source)

    def test_links_installed_during_a_call_never_receive_writes(self):
        original = self.source.read_bytes()
        real = CLIProvider.complete
        for name, error in [('manifest.json.tmp', None), ('journal.jsonl', ProviderError('rate_limit'))]:
            for kind in ('hard', 'symbolic'):
                with self.subTest(name=name, kind=kind):
                    prepared = self.prepared()
                    def attack(provider, context, schema, **kwargs):
                        self.link(kind, name)
                        if error: raise error
                        return real(provider, context, schema, **kwargs)
                    with patch.object(CLIProvider, 'complete', attack):
                        with self.assertRaisesRegex(ValueError, 'linked files'):
                            run(self.output, prepared['approval_hash'])
                    self.assertEqual(self.source.read_bytes(), original)
                    shutil.rmtree(self.output)

    def test_storage_refuses_links_even_without_directory_guard(self):
        original = self.source.read_bytes()
        self.output.mkdir()
        store = ImproveStore(self.output)
        with patch('skill_evolver.improve.guard_directory'):
            for kind in ('hard', 'symbolic'):
                with self.subTest(kind=kind):
                    self.link(kind, 'manifest.json.tmp')
                    store.write('manifest.json', {'state': 'incomplete'})
                    self.link(kind, 'journal.jsonl')
                    with self.assertRaises(OSError if kind == 'symbolic' else ValueError):
                        store.append({'kind': 'call_finished'})
        self.assertEqual(self.source.read_bytes(), original)

    def test_rejected_proposals_consume_iterations(self):
        real = CLIProvider.complete
        for name, content in [('malformed', ['not an object']), ('prompt_limit', {'skill_text': SKILL + '$' * 60000})]:
            with self.subTest(case=name):
                prepared = self.prepared()
                def propose(provider, context, schema, **kwargs):
                    result = real(provider, context, schema, **kwargs)
                    return {**result, 'content': content} if 'current_skill' in context else result
                with patch.object(CLIProvider, 'complete', propose):
                    result = run(self.output, prepared['approval_hash'])
                self.assertEqual(result['status'], 'no_observed_improvement')
                self.assertNotIn('proposed_skill', ''.join(map(json.dumps, self.calls())))
                self.assertEqual(len(self.calls()), 16)
                shutil.rmtree(self.output); self.log.unlink()

    def test_comparison_without_recorded_completion_is_not_reported(self):
        prepared = self.prepared()
        run(self.output, prepared['approval_hash'])
        manifest = json.loads((self.output/'manifest.json').read_text())
        manifest['state'] = 'running'  # crash after comparison.json, before completion
        (self.output/'manifest.json').write_text(json.dumps(manifest))
        result = report(self.output)
        self.assertEqual(result['status'], 'incomplete')
        self.assertNotIn('comparison', result)
        self.assertNotIn('observed_improvement', (self.output/'comparison.md').read_text())

    def test_paths_original_mutation_and_scope_rejection(self):
        with self.assertRaises(ValueError):
            prepare(self.source, 'claude', 'claude-test-1', self.source.parent/'run')
        prepared = self.prepared()
        self.source.write_text(SKILL+'Changed')
        with self.assertRaises(ValueError): run(self.output, prepared['approval_hash'])
        self.source.write_text(SKILL)
        (self.output/'evil').symlink_to(self.source)
        with self.assertRaises(ValueError): report(self.output)
        (self.output/'evil').unlink()
        self.assertEqual(self.source.read_text(), SKILL)
        os.link(self.source, self.output/'manifest.json.tmp')
        with self.assertRaises(ValueError): report(self.output)
        self.assertEqual(self.source.read_text(), SKILL)

    def test_unsupported_scope_is_not_success(self):
        unsupported = evaluation(); unsupported.update(execution_scope='unsupported',
            unsupported_reason='Browser actions are essential', development=[], heldout=[])
        with patch.dict(os.environ, {'FAKE_EVAL':json.dumps(unsupported)}):
            result = self.prepared()
        self.assertTrue(result['unsupported'])
        with self.assertRaises(ValueError): run(self.output, result['approval_hash'])
        self.assertEqual(len(self.calls()), 1)

    def test_valid_large_comparison_report_remains_readable(self):
        prepared = self.prepared()
        completed = run(self.output, prepared['approval_hash'])
        comparison = completed['comparison']
        for split in ('original_heldout', 'best_heldout'):
            for row in comparison[split]:
                for criterion in row['criteria']:
                    criterion['reason'] = 'x' * 200000
        path = self.output/'comparison.json'
        path.write_text(json.dumps(comparison))
        self.assertGreater(path.stat().st_size, 2 * 1024 * 1024)
        self.assertEqual(report(self.output)['status'], 'observed_improvement')

    def test_missing_candidate_diff_is_not_hidden_in_report(self):
        prepared = self.prepared()
        completed = run(self.output, prepared['approval_hash'])
        record_path = self.output/'candidates'/(completed['comparison']['recommended_id']+'.json')
        record = json.loads(record_path.read_text())
        del record['diff']
        record_path.write_text(json.dumps(record))
        with self.assertRaises(KeyError): report(self.output)

    def test_manifest_requires_an_object(self):
        self.prepared()
        (self.output/'manifest.json').write_text('[]')
        with self.assertRaises(ValueError): report(self.output)

    def test_cli_prepare_report_and_error_exit(self):
        with patch('sys.stdout', new_callable=io.StringIO) as stdout, patch('sys.stderr', new_callable=io.StringIO):
            code = main(['improve','prepare',str(self.source),'--provider','claude','--model','claude-test-1','--output',str(self.output),'--json'])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout.getvalue())['state'], 'draft')
        with patch('sys.stdout', new_callable=io.StringIO):
            self.assertEqual(main(['improve','report',str(self.output)]), 0)
        with patch('sys.stderr', new_callable=io.StringIO):
            self.assertEqual(main(['improve','run',str(self.output),'--approve-evaluation','bad']), 1)

    def cli(self, *args, answer=None, tty=False):
        with patch('sys.stdout', new_callable=io.StringIO) as stdout, \
                patch('sys.stderr', new_callable=io.StringIO) as stderr, \
                patch('sys.stdin.isatty', return_value=tty), patch('sys.stdout.isatty', return_value=tty, create=True), \
                patch('builtins.input', side_effect=answer if callable(answer) else lambda prompt: answer) as asked:
            code = main(['improve', *map(str, args)])
        return code, stdout.getvalue(), stderr.getvalue(), asked.called

    def test_human_output_guides_each_step(self):
        with patch.dict(os.environ, {'LANG': 'en_US.UTF-8', 'LC_ALL': '', 'LC_MESSAGES': ''}):
            code, out, err, _ = self.cli('prepare', self.source, '--provider', 'claude', '--model', 'claude-test-1',
                                         '--output', self.output)
            self.assertEqual(code, 0)
            self.assertIn('State: waiting for your review', out)
            self.assertIn('skill-evolver improve run ' + str(self.output), out)
            self.assertIn('model call 1', err)
        with patch.dict(os.environ, {'LANG': 'ja_JP.UTF-8', 'LC_ALL': '', 'LC_MESSAGES': ''}):
            code, out, _, _ = self.cli('report', self.output)
            self.assertIn('状態: 評価案の確認待ち', out)
            self.assertIn('評価案を読む', out)

    def test_interactive_approval_binds_the_displayed_evaluation(self):
        approval = self.prepared()['approval_hash']
        before = len(self.calls())
        code, _, err, asked = self.cli('run', self.output)
        self.assertEqual((code, asked, len(self.calls())), (1, False, before))
        self.assertIn('approval required', err)
        prompts = []
        code, out, _, asked = self.cli('run', self.output, tty=True, answer=lambda p: prompts.append(p) or 'n')
        self.assertEqual((code, asked, len(self.calls())), (1, True, before))
        self.assertIn(approval, out)
        self.assertIn('59', prompts[0])
        def interrupt(prompt):
            raise KeyboardInterrupt
        self.assertEqual(self.cli('run', self.output, tty=True, answer=interrupt)[0], 1)
        self.assertFalse((self.output / 'approved.json').exists())
        with patch.dict(os.environ, {'LANG': 'en_US.UTF-8', 'LC_ALL': '', 'LC_MESSAGES': ''}):
            code, out, _, _ = self.cli('run', self.output, tty=True, answer='y')
        self.assertEqual(code, 0)
        self.assertIn('improvement observed', out)
        self.assertIn('Held-out:    original 3/6 -> best revision 6/6', out)
        self.assertIn('cp ', out)
        self.assertEqual(report(self.output)['comparison']['approval_hash'], approval)
        self.assertEqual(self.source.read_text(), SKILL)

    def test_saturated_development_skips_revisions_without_changing_the_result(self):
        easy = evaluation(); easy['criteria'] = easy['criteria'][:1]
        with patch.dict(os.environ, {'FAKE_EVAL': json.dumps(easy)}):
            result = run(self.output, self.prepared()['approval_hash'])
        self.assertEqual(result['status'], 'no_observed_improvement')
        self.assertFalse(result['revision_selected'])
        # prepare + original development (6) + original held-out (6); no proposals.
        self.assertEqual(len(self.calls()), 13)
        self.assertIn('search_stopped', [e['kind'] for e in ImproveStore(self.output).events()])
        with patch.dict(os.environ, {'LANG': 'en_US.UTF-8', 'LC_ALL': '', 'LC_MESSAGES': ''}):
            code, out, _, _ = self.cli('report', self.output)
        self.assertIn('revisions were skipped', out)
        self.assertNotIn('no revision scored higher', out)

    def test_edit_while_confirming_is_not_run(self):
        self.prepared()
        before = len(self.calls())
        edited = evaluation(); edited['purpose'] = 'Changed after display'
        def edit(prompt):
            (self.output / 'evaluation.json').write_text(json.dumps(edited))
            return 'y'
        code, _, err, _ = self.cli('run', self.output, tty=True, answer=edit)
        self.assertEqual((code, len(self.calls())), (1, before))
        self.assertIn('approval hash differs', err)
        self.assertFalse((self.output / 'approved.json').exists())

    def test_terminal_output_escapes_model_control_characters(self):
        hidden = evaluation(); hidden['purpose'] = 'Visible\x1b[8mhidden\u202e'
        with patch.dict(os.environ, {'FAKE_EVAL': json.dumps(hidden)}):
            self.prepared()
        code, out, _, _ = self.cli('run', self.output, tty=True, answer='n')
        self.assertEqual(code, 1)
        self.assertNotIn('\x1b', out)
        self.assertNotIn('\u202e', out)
        self.assertIn('Visible\\x1b[8mhidden\\u202e', out)

    def test_untested_cli_version_requires_opt_in_and_is_reported(self):
        with patch.dict('skill_evolver.cli_provider.SUPPORTED', {'claude': '9.9.9'}):
            code, _, err, _ = self.cli('prepare', self.source, '--provider', 'claude', '--model', 'claude-test-1',
                                       '--output', self.output)
            self.assertEqual(code, 1)
            self.assertIn('--allow-untested-cli', err)
            self.assertEqual(self.calls(), [])
            prepared = prepare(self.source, 'claude', 'claude-test-1', self.output, allow_untested=True)
            self.assertFalse(load_bundle(self.output)[1]['provider_profile']['version_tested'])
            result = run(self.output, prepared['approval_hash'])
        self.assertEqual(result['status'], 'observed_improvement')
        self.assertTrue(any('untested' in item for item in result['warnings']))

    def test_perfect_original_score_explains_why_no_improvement_is_possible(self):
        from skill_evolver.improve_text import render
        row = {'case_id': 'h', 'criteria': [{'id': 'purpose', 'passed': True}]}
        summary = {'state': 'completed', 'directory': str(self.output), 'source_path': str(self.source),
                   'revision_selected': True, 'required_criteria': ['purpose'],
                   'comparison': {'status': 'no_observed_improvement', 'baseline_development': [row],
                                  'best_development': [row], 'original_heldout': [row], 'best_heldout': [row]}}
        with patch.dict(os.environ, {'LANG': 'en_US.UTF-8', 'LC_ALL': '', 'LC_MESSAGES': ''}):
            text = render(summary)
        self.assertIn('already passes every held-out check', text)
        self.assertNotIn('cp ', text)
        fail = {'case_id': 'h', 'criteria': [{'id': 'purpose', 'passed': False}, {'id': 'quality', 'passed': True},
                                             {'id': 'style', 'passed': True}]}
        base = {'case_id': 'h', 'criteria': [{'id': 'purpose', 'passed': True}, {'id': 'quality', 'passed': False},
                                             {'id': 'style', 'passed': False}]}
        summary['comparison'].update(original_heldout=[base], best_heldout=[fail])
        summary.update(revision_selected=True, required_criteria=['purpose'])
        with patch.dict(os.environ, {'LANG': 'en_US.UTF-8', 'LC_ALL': '', 'LC_MESSAGES': ''}):
            text = render(summary)
        self.assertIn('newly fails required checks: h/purpose', text)

    def test_errors_carry_an_actionable_hint(self):
        self.output.mkdir()
        with patch.dict(os.environ, {'LANG': 'en_US.UTF-8', 'LC_ALL': '', 'LC_MESSAGES': ''}):
            code, _, err, _ = self.cli('prepare', self.source, '--provider', 'claude', '--model', 'claude-test-1',
                                       '--output', self.output)
        self.assertEqual(code, 1)
        self.assertIn('Choose a new --output directory', err)

    def test_reported_model_drift_and_missing_metadata_stop_comparison(self):
        prepared = self.prepared()
        with patch.dict(os.environ, {'FAKE_REPORTED_MODEL':'claude-different-model'}):
            result = run(self.output, prepared['approval_hash'])
        self.assertEqual(result['status'], 'incomplete')
        self.assertEqual(result['reason'], 'reported_model_changed_or_missing')
        self.assertEqual(len(self.calls()), 2)
        self.assertEqual(self.source.read_text(), SKILL)

    def test_missing_model_metadata_does_not_resolve_an_alias(self):
        with patch.dict(os.environ, {'FAKE_NO_MODEL':'1'}):
            provider=CLIProvider('claude','sonnet'); provider.inspect()
            with self.assertRaisesRegex(ProviderError,'unresolved_model_alias'):
                provider.complete({'task':'text only'},ev.OUTPUT_SCHEMA)

    def test_owned_process_group_is_reaped_after_timeout(self):
        child_file=self.root/'child.pid'
        script=('import pathlib,subprocess,sys,time; '
                'p=subprocess.Popen([sys.executable,"-c","import time; time.sleep(20)"]); '
                'pathlib.Path(sys.argv[1]).write_text(str(p.pid)); time.sleep(20)')
        with self.assertRaisesRegex(ProviderError,'timeout'):
            bounded_process([sys.executable,'-c',script,str(child_file)],cwd=self.root,
                            env=login_environment(),timeout=.3)
        child=int(child_file.read_text())
        status=subprocess.run(['ps','-o','stat=','-p',str(child)],capture_output=True,text=True).stdout.strip()
        if status and not status.startswith('Z'):
            os.kill(child,signal.SIGKILL)
        self.assertTrue(not status or status.startswith('Z'),status)

    def test_timeout_flood_and_bad_json(self):
        provider = CLIProvider('claude','claude-test-1'); provider.inspect()
        for mode, reason, timeout in [('timeout','timeout',1),('flood','output_limit',10),('badjson','invalid_provider_output',10)]:
            with self.subTest(mode=mode), patch.dict(os.environ, {'FAKE_MODE':mode}):
                with self.assertRaisesRegex(ProviderError, reason):
                    provider.complete({'task':'test'},ev.OUTPUT_SCHEMA,timeout=timeout,stream_limit=1024)


class EvaluationContractTests(unittest.TestCase):
    def test_reject_bad_cases_and_scores(self):
        for mutate in [lambda e:e.update(extra=1), lambda e:e['heldout'][0].update(group_id='devg0'),
                       lambda e:e['heldout'][0].update(prompt='Development 0'),
                       lambda e:e['criteria'][0].update(required=1)]:
            data = evaluation(); mutate(data)
            with self.assertRaises(ValueError): ev.validate_evaluation(data)
        with self.assertRaises(ValueError):
            ev.validate_response({'criteria':[{'id':'not-known','passed':True,'reason':'x'}]},'judgment',evaluation())

    def test_required_regression_blocks_higher_score(self):
        e = evaluation()
        a = [{'case_id':'a','criteria':[{'id':'purpose','passed':True},{'id':'quality','passed':False}]}]
        b = [{'case_id':'a','criteria':[{'id':'purpose','passed':False},{'id':'quality','passed':True}]},
             {'case_id':'b','criteria':[{'id':'purpose','passed':True},{'id':'quality','passed':True}]}]
        self.assertFalse(ev.better(b,a,e))
        self.assertFalse(ev.better(a,a,e))

    def test_provider_parsers_reject_malformed_events(self):
        with self.assertRaises(ValueError):
            CLIProvider('codex','gpt-test-1')._parse([{'missing':'type'}])
        with self.assertRaises(ValueError):
            CLIProvider('claude','claude-test-1')._parse([{'type':'assistant','message':[]}])

    def test_environment_does_not_change_login_locations(self):
        with patch.dict(os.environ, {'OPENAI_BASE_URL':'override','CODEX_HOME':'/preserved','HOME':'/home'}):
            environment = login_environment()
        self.assertNotIn('OPENAI_BASE_URL',environment)
        self.assertEqual(environment['CODEX_HOME'],'/preserved')
        self.assertEqual(environment['HOME'],'/home')


if __name__ == '__main__': unittest.main()
