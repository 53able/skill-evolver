"""Provider contracts against generated fake CLIs; nothing here reaches a real model."""
import json
import os
from pathlib import Path
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

from skill_evolver import cli_provider
from skill_evolver.cli_provider import (CLIProvider, ProviderError, PROFILE_VERSION, SCHEMA_SLOT,
                                        bounded_process, login_environment)


SRC = str(Path(cli_provider.__file__).resolve().parents[1])
HELP = ('--safe-mode --restricted --tools --strict-mcp-config --json-schema --output-format '
        '--no-session-persistence --ignore-user-config --ignore-rules --ephemeral --output-schema --json --sandbox')
FAKE = r'''#!/usr/bin/env python3
import json, os, sys
from pathlib import Path
name, args, env = Path(sys.argv[0]).name, sys.argv[1:], os.environ
if args == ['--version']:
    print(env.get('FAKE_VERSION') or {'claude': 'Claude Code 2.1.310', 'codex': 'codex-cli 0.164.0'}[name]); sys.exit()
if '--help' in args:
    print(env['FAKE_HELP']); sys.exit()
if args[:2] == ['auth', 'status']:
    print(env.get('FAKE_AUTH') or json.dumps({'loggedIn': True, 'authMethod': 'claude.ai'})); sys.exit()
if args[:2] == ['login', 'status']:
    print(env.get('FAKE_LOGIN') or 'Logged in using ChatGPT', file=sys.stderr); sys.exit()
record = {'argv': args, 'stdin': sys.stdin.read()}
if '--output-schema' in args:
    record['schema_file'] = Path(args[args.index('--output-schema') + 1]).read_text()
with open(env['FAKE_LOG'], 'a') as log:
    log.write(json.dumps(record) + '\n')
sys.stdout.write(env['FAKE_EVENTS'])
'''
INIT = {'type': 'system', 'subtype': 'init', 'tools': ['StructuredOutput'], 'mcp_servers': [],
        'model': 'claude-test-1'}
RESULT = {'type': 'result', 'subtype': 'success', 'is_error': False, 'structured_output': {'output': 'ok'},
          'usage': {'input_tokens': 1, 'output_tokens': 1}}
CODEX = [{'type': 'thread.started'},
         {'type': 'item.completed', 'item': {'type': 'agent_message', 'text': '{"output":"ok"}'}},
         {'type': 'turn.completed', 'usage': {'input_tokens': 1, 'output_tokens': 1}}]
SCHEMA = {'type': 'object', 'properties': {'output': {'type': 'string'}}}


def jsonl(*events):
    return ''.join(json.dumps(event) + '\n' for event in events)


def gone(pid, wait=3):
    """True once the PID is absent or a zombie; kills it on failure so tests never leak."""
    end = time.monotonic() + wait
    while True:
        status = subprocess.run(['ps', '-o', 'stat=', '-p', str(pid)], capture_output=True, text=True).stdout.strip()
        if not status or status.startswith('Z'):
            return True
        if time.monotonic() >= end:
            break
        time.sleep(.05)
    os.kill(pid, signal.SIGKILL)
    return False


def read_pids(path, count, wait=5):
    end = time.monotonic() + wait
    while time.monotonic() < end:
        if path.exists() and len(path.read_text().split()) == count:
            return [int(pid) for pid in path.read_text().split()]
        time.sleep(.02)
    raise AssertionError('child did not report its PIDs')


# Leader spawns a same-group grandchild that holds no pipes, records both PIDs, then runs ACTION.
FAMILY = ('import os,pathlib,signal,subprocess,sys,time; '
          'g=subprocess.Popen([sys.executable,"-c","import time; time.sleep(20)"],'
          'stdout=subprocess.DEVNULL,stderr=subprocess.DEVNULL); '
          'pathlib.Path(sys.argv[1]).write_text("%d %d" % (os.getpid(), g.pid)); ACTION')


def family(action):
    return FAMILY.replace('ACTION', action)


class FakeCLI(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.bin = self.root / 'bin'; self.bin.mkdir()
        for name in ('claude', 'codex'):
            executable = self.bin / name; executable.write_text(FAKE); executable.chmod(0o755)
        self.log = self.root / 'calls.jsonl'
        # Only the fake directory precedes system tools, so a real CLI is never resolved.
        self.environment = patch.dict(os.environ, {
            'PATH': str(self.bin) + os.pathsep + os.environ['PATH'], 'FAKE_LOG': str(self.log),
            'FAKE_HELP': HELP, 'FAKE_EVENTS': jsonl(INIT, RESULT)})
        self.environment.start()

    def tearDown(self):
        self.environment.stop(); self.temporary.cleanup()

    def calls(self):
        return [json.loads(line) for line in self.log.read_text().splitlines()] if self.log.exists() else []

    def events(self, *events):
        return patch.dict(os.environ, {'FAKE_EVENTS': jsonl(*events)})


class ProfileTests(FakeCLI):
    def test_profile_freezes_full_argv_with_one_schema_slot(self):
        for name, model in [('claude', 'claude-test-1'), ('codex', 'gpt-test-1')]:
            with self.subTest(name=name):
                profile = CLIProvider(name, model).inspect()
                self.assertEqual(profile['profile_version'], PROFILE_VERSION)
                self.assertEqual(profile['argv'][0], profile['executable'])
                self.assertEqual(profile['argv'].count(SCHEMA_SLOT), 1)
                self.assertEqual(profile['argv'][profile['argv'].index('--model') + 1], model)
                self.assertNotIn('controls', profile)
                self.assertEqual(CLIProvider(name, model).inspect(), profile)

    def test_executed_argv_is_the_frozen_argv_with_only_the_schema_substituted(self):
        other = {'type': 'object', 'properties': {'other': {'type': 'string'}}}
        for name, model, events in [('claude', 'claude-test-1', (INIT, RESULT)), ('codex', 'gpt-test-1', CODEX)]:
            with self.subTest(name=name), self.events(*events):
                self.log.unlink(missing_ok=True)
                provider = CLIProvider(name, model); profile = provider.inspect()
                provider.complete({'task': 'a'}, SCHEMA); provider.complete({'task': 'b'}, other)
                for call, schema in zip(self.calls(), [SCHEMA, other]):
                    slot = profile['argv'].index(SCHEMA_SLOT)
                    self.assertEqual(call['argv'][:slot - 1] + call['argv'][slot:],
                                     profile['argv'][1:slot] + profile['argv'][slot + 1:])
                    if name == 'claude':
                        self.assertEqual(json.loads(call['argv'][slot - 1]), schema)
                    else:
                        self.assertEqual(Path(call['argv'][slot - 1]).name, 'response-schema.json')
                        self.assertEqual(json.loads(call['schema_file']), schema)
                    self.assertNotIn(SCHEMA_SLOT, call['argv'])

    def test_frozen_argv_drift_is_rejected_before_inference(self):
        profile = CLIProvider('claude', 'claude-test-1').inspect()
        for argv in [profile['argv'] + ['--extra'], [part for part in profile['argv'] if part != '--restricted']]:
            with self.assertRaisesRegex(ProviderError, 'provider_profile_changed'):
                CLIProvider('claude', 'claude-test-1', frozen={**profile, 'argv': argv}).inspect()
        tampered = CLIProvider('claude', 'claude-test-1'); tampered.inspect()
        tampered.profile = {**tampered.profile, 'argv': [p for p in profile['argv'] if p != SCHEMA_SLOT]}
        with self.assertRaisesRegex(ProviderError, 'provider_profile_changed'):
            tampered.complete({'task': 'x'}, SCHEMA)
        self.assertEqual(self.calls(), [])

    def test_selectors_that_parse_as_options_are_rejected(self):
        for name in ('claude', 'codex'):
            for model in ('-p', '--dangerously-skip-permissions', '-c=web_search="live"'):
                with self.subTest(name=name, model=model), \
                        self.assertRaisesRegex(ProviderError, 'provider_and_explicit_model_required'):
                    CLIProvider(name, model)

    def test_codex_alias_is_rejected_before_any_process_starts(self):
        with patch.dict(os.environ, {'PATH': str(self.root / 'empty')}):
            for model in ('gpt-5-latest', 'o3', 'default', 'codex-mini'):
                with self.subTest(model=model), self.assertRaisesRegex(ProviderError, 'unresolved_model_alias'):
                    CLIProvider('codex', model)
            with self.assertRaisesRegex(ProviderError, 'cli_not_installed:codex'):
                CLIProvider('codex', 'gpt-test-1').inspect()
        self.assertEqual(self.calls(), [])

    def test_claude_alias_resolves_only_through_reported_id(self):
        provider = CLIProvider('claude', 'sonnet'); provider.inspect()
        with self.events({**INIT, 'model': 'claude-sonnet-test-1'}, RESULT):
            result = provider.complete({'task': 'x'}, SCHEMA)
        self.assertEqual((result['requested_model'], result['reported_model'], result['model_identity']),
                         ('sonnet', 'claude-sonnet-test-1', 'reported'))
        fixed = CLIProvider('claude', 'claude-test-1'); fixed.inspect()
        with self.events({key: value for key, value in INIT.items() if key != 'model'}, RESULT):
            self.assertEqual(fixed.complete({'task': 'x'}, SCHEMA)['model_identity'], 'requested_only')

    def test_missing_unsupported_or_api_key_cli_is_refused(self):
        cases = [
            ('claude', {'PATH': str(self.root / 'empty')}, 'cli_not_installed:claude'),
            ('claude', {'FAKE_VERSION': 'Claude Code 2.1.311'}, 'unsupported_cli_version:claude'),
            ('codex', {'FAKE_VERSION': 'codex-cli 0.164.1'}, 'unsupported_cli_version:codex'),
            ('claude', {'FAKE_HELP': HELP.replace('--safe-mode', '')}, 'unsupported_cli_capability'),
            ('claude', {'FAKE_AUTH': json.dumps({'loggedIn': True, 'authMethod': 'api_key'})},
             'subscription_login_required:claude'),
            ('claude', {'FAKE_AUTH': json.dumps({'loggedIn': False})}, 'subscription_login_required:claude'),
            ('codex', {'FAKE_LOGIN': 'Logged in using an API key'}, 'subscription_login_required:codex'),
        ]
        for name, environment, reason in cases:
            with self.subTest(reason=reason, environment=environment), patch.dict(os.environ, environment), \
                    self.assertRaisesRegex(ProviderError, '^' + reason + '$'):
                CLIProvider(name, 'claude-test-1' if name == 'claude' else 'gpt-test-1').inspect()
        self.assertEqual(self.calls(), [])


    def test_untested_version_is_opt_in_and_frozen(self):
        with patch.dict(os.environ, {'FAKE_VERSION': 'Claude Code 2.1.311'}):
            with self.assertRaisesRegex(ProviderError, '^unsupported_cli_version:claude$'):
                CLIProvider('claude', 'claude-test-1').inspect()
            profile = CLIProvider('claude', 'claude-test-1', allow_untested=True).inspect()
            self.assertEqual((profile['cli_version'], profile['version_tested']), ('2.1.311', False))
            self.assertEqual(CLIProvider('claude', 'claude-test-1', frozen=profile, allow_untested=True).inspect(), profile)
        self.assertNotIn('version_tested', CLIProvider('claude', 'claude-test-1').inspect())
        with patch.dict(os.environ, {'FAKE_VERSION': 'Claude Code 2.1.311', 'FAKE_HELP': HELP.replace('--safe-mode', '')}), \
                self.assertRaisesRegex(ProviderError, 'unsupported_cli_capability'):
            CLIProvider('claude', 'claude-test-1', allow_untested=True).inspect()
        self.assertEqual(self.calls(), [])


class ClaudeInventoryTests(FakeCLI):
    def parse(self, *events):
        return CLIProvider('claude', 'claude-test-1')._parse(list(events))

    def test_valid_init_is_accepted(self):
        self.assertEqual(self.parse(INIT, RESULT)[2], 'claude-test-1')
        self.assertEqual(self.parse({**INIT, 'tools': []}, RESULT)[0], {'output': 'ok'})

    def test_missing_duplicate_or_incomplete_inventory_fails_closed(self):
        without = lambda key: {k: v for k, v in INIT.items() if k != key}
        for events in [(RESULT,), (INIT, INIT, RESULT), (without('tools'), RESULT),
                       (without('mcp_servers'), RESULT), ({**INIT, 'tools': 'StructuredOutput'}, RESULT),
                       ({**INIT, 'tools': [{'name': 'Bash'}]}, RESULT), ({**INIT, 'mcp_servers': {}}, RESULT)]:
            with self.subTest(events=events), self.assertRaisesRegex(ProviderError, 'unverified_tool_inventory'):
                self.parse(*events)

    def test_forbidden_tools_or_mcp_servers_are_rejected(self):
        for init in [{**INIT, 'tools': ['StructuredOutput', 'Bash']}, {**INIT, 'tools': ['mcp__sentinel__read']},
                     {**INIT, 'mcp_servers': [{'name': 'sentinel', 'status': 'connected'}]}]:
            with self.subTest(init=init), self.assertRaisesRegex(ProviderError, 'unexpected_available_tools'):
                self.parse(init, RESULT)

    def test_missing_init_is_not_accepted_through_complete(self):
        provider = CLIProvider('claude', 'claude-test-1'); provider.inspect()
        with self.events(RESULT), self.assertRaisesRegex(ProviderError, 'unverified_tool_inventory'):
            provider.complete({'task': 'x'}, SCHEMA)


class ProcessTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.root = Path(self.temporary.name)
        self.pids = self.root / 'pids'

    def tearDown(self):
        self.temporary.cleanup()

    def test_parent_sigterm_or_sighup_reaps_group_and_keeps_signal_exit(self):
        parent_script = ('import sys; from skill_evolver.cli_provider import bounded_process, login_environment; '
                         'bounded_process([sys.executable, "-c", sys.argv[1], sys.argv[2]], cwd=sys.argv[3], '
                         'env=login_environment(), timeout=30)')
        for signum in (signal.SIGTERM, signal.SIGHUP):
            with self.subTest(signum=signum):
                self.pids.unlink(missing_ok=True)
                parent = subprocess.Popen([sys.executable, '-c', parent_script, family('time.sleep(20)'),
                                           str(self.pids), str(self.root)], env={**os.environ, 'PYTHONPATH': SRC})
                try:
                    leader, grandchild = read_pids(self.pids, 2)
                    parent.send_signal(signum)
                    self.assertEqual(parent.wait(timeout=10), -signum)
                finally:
                    parent.kill(); parent.wait()
                self.assertTrue(gone(leader), 'leader survived parent signal')
                self.assertTrue(gone(grandchild), 'descendant survived parent signal')

    def test_prior_python_handler_runs_after_reap_and_handlers_are_restored(self):
        seen = []
        def prior(signum, frame):
            seen.append((signum, gone(int(self.pids.read_text().split()[1]), wait=0)))
        saved = signal.signal(signal.SIGTERM, prior)
        saved_hup = signal.getsignal(signal.SIGHUP)
        try:
            with self.assertRaisesRegex(ProviderError, 'interrupted'):
                bounded_process([sys.executable, '-c', family('os.kill(os.getppid(), signal.SIGTERM); time.sleep(20)'),
                                 str(self.pids)], cwd=self.root, env=login_environment(), timeout=30)
            self.assertEqual(seen, [(signal.SIGTERM, True)])
            self.assertIs(signal.getsignal(signal.SIGTERM), prior)
            self.assertIs(signal.getsignal(signal.SIGHUP), saved_hup)
        finally:
            signal.signal(signal.SIGTERM, saved)

    def test_ignored_signal_stays_ignored(self):
        saved = signal.signal(signal.SIGHUP, signal.SIG_IGN)
        try:
            code, out, _ = bounded_process([sys.executable, '-c', 'import os,signal; os.kill(os.getppid(), signal.SIGHUP); print("done")'],
                                           cwd=self.root, env=login_environment(), timeout=30)
            self.assertEqual((code, out), (0, b'done\n'))
            self.assertIs(signal.getsignal(signal.SIGHUP), signal.SIG_IGN)
        finally:
            signal.signal(signal.SIGHUP, saved)

    def test_worker_thread_runs_without_touching_signal_handlers(self):
        results = []
        def work():
            results.append(bounded_process([sys.executable, '-c', 'print(1)'], cwd=self.root,
                                           env=login_environment(), timeout=30))
        with patch.object(signal, 'signal', side_effect=AssertionError('handler installed off main thread')):
            worker = threading.Thread(target=work); worker.start(); worker.join(30)
        self.assertEqual(results, [(0, b'1\n', b'')])

    def test_group_is_signalled_before_leader_is_reaped_and_descendants_die(self):
        real, observed = os.killpg, []
        def killpg(pgid, signum):
            # The leader is listed with our PID as parent only until it is reaped.
            ppid = subprocess.run(['ps', '-o', 'ppid=', '-p', str(pgid)], capture_output=True, text=True).stdout
            observed.append(ppid.strip() == str(os.getpid()))
            return real(pgid, signum)
        with patch.object(cli_provider.os, 'killpg', killpg):
            code, _, _ = bounded_process([sys.executable, '-c', family('sys.exit(3)'), str(self.pids)],
                                         cwd=self.root, env=login_environment(), timeout=30)
        self.assertEqual(code, 3)
        self.assertEqual(observed, [True, True])
        self.assertTrue(gone(read_pids(self.pids, 2)[1]), 'descendant survived normal exit')


if __name__ == '__main__': unittest.main()
