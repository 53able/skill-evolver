"""Bounded, subscription-authenticated Claude Code and Codex subprocesses."""
import contextlib
import errno
import json
import os
from pathlib import Path
import re
import select
import selectors
import shutil
import signal
import subprocess
import tempfile
import threading
import time


SUPPORTED = {'claude': '2.1.310', 'codex': '0.164.0'}
PROFILE_VERSION = 2
# The only per-call value in the frozen invocation argv.
SCHEMA_SLOT = '<response-schema>'
BOUNDARY = ('Model shell/MCP/web/write actions are restricted. Managed policy hooks, '
            'CLI authentication/internal state, and complete host-read isolation are not covered.')


class ProviderError(ValueError):
    def __init__(self, reason):
        super().__init__(reason)
        self.reason = reason


def load_json(text):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result:
                raise ValueError('duplicate JSON key')
            result[key] = value
        return result
    return json.loads(text, object_pairs_hook=pairs,
                      parse_constant=lambda value: (_ for _ in ()).throw(ValueError('non-finite JSON')))


def login_environment():
    """Retain CLI-managed login locations; remove key/endpoint/config overrides."""
    excluded = ('ANTHROPIC_', 'OPENAI_', 'AZURE_', 'CLAUDE_CODE_USE_', 'CODEX_API_')
    exact = {'CLAUDE_CODE_OAUTH_TOKEN', 'CLAUDECODE', 'CLAUDE_CODE_ENTRYPOINT',
             'CLAUDE_CODE_SIMPLE', 'CLAUDE_CODE_SETTINGS', 'CLAUDE_CODE_BARE',
             'CODEX_CONFIG', 'CODEX_PROFILE', 'GOOGLE_API_KEY', 'GEMINI_API_KEY'}
    return {key: value for key, value in os.environ.items()
            if key not in exact and not key.startswith(excluded)}


class _ParentSignal(BaseException):
    """Unwinds the read loop so the owned group is reaped before the parent exits."""


@contextlib.contextmanager
def _reap_on_parent_signal():
    """Turn SIGTERM/SIGHUP into cleanup, then re-deliver them under the prior handler."""
    state = {'armed': False, 'received': None}

    def handler(signum, frame):
        state['received'] = state['received'] or signum
        if state['armed']:
            state['armed'] = False
            raise _ParentSignal

    previous = {}
    # Handlers are process-wide and installable only from the main thread.
    if threading.current_thread() is threading.main_thread():
        for signum in (signal.SIGTERM, signal.SIGHUP):
            current = signal.getsignal(signum)
            if current is signal.SIG_DFL or callable(current):
                previous[signum] = signal.signal(signum, handler)
    try:
        yield state
    except _ParentSignal:
        pass
    finally:
        for signum, current in previous.items():
            signal.signal(signum, current)
        if state['received']:
            # Default disposition still terminates the parent with this signal.
            signal.raise_signal(state['received'])
            raise ProviderError('interrupted')


def _exited(pid, timeout):
    """Wait without reaping: an unreaped leader keeps its PID and process group reserved."""
    if hasattr(os, 'waitid'):
        end = time.monotonic() + timeout
        while os.waitid(os.P_PID, pid, os.WEXITED | os.WNOHANG | os.WNOWAIT) is None:
            if time.monotonic() >= end:
                return False
            time.sleep(.01)
        return True
    # macOS before Python 3.13 has no waitid; kqueue observes the exit without reaping.
    with contextlib.closing(select.kqueue()) as queue:
        ready = queue.control([select.kevent(pid, select.KQ_FILTER_PROC, select.KQ_EV_ADD | select.KQ_EV_ONESHOT,
                                             select.KQ_NOTE_EXIT)], 1, timeout)
    # ESRCH for our own unreaped child means it is already a zombie.
    if ready and ready[0].flags & select.KQ_EV_ERROR and ready[0].data != errno.ESRCH:
        raise OSError(ready[0].data, os.strerror(ready[0].data))
    return bool(ready)


def _signal_group(pid, signum):
    try:
        os.killpg(pid, signum)
    except (ProcessLookupError, PermissionError):
        # macOS reports EPERM when only the unreaped leader remains in the group.
        pass


def bounded_process(argv, *, cwd, env, input_bytes=b'', timeout=120, limit=1024 * 1024):
    """Limit both pipes and the whole owned process group, including descendants."""
    with tempfile.TemporaryFile() as source, _reap_on_parent_signal() as guard:
        source.write(input_bytes)
        source.seek(0)
        process = subprocess.Popen(argv, stdin=source, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, cwd=cwd, env=env,
                                   start_new_session=True)
        selector = selectors.DefaultSelector()
        buffers = {'stdout': bytearray(), 'stderr': bytearray()}
        for name, pipe in [('stdout', process.stdout), ('stderr', process.stderr)]:
            selector.register(pipe, selectors.EVENT_READ, name)
        deadline = time.monotonic() + timeout
        try:
            guard['armed'] = True
            if guard['received']:
                raise _ParentSignal
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise ProviderError('timeout')
                for key, _ in selector.select(min(remaining, .1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fileobj)
                    else:
                        buffers[key.data].extend(chunk)
                        if len(buffers[key.data]) > limit:
                            raise ProviderError('output_limit')
            if not _exited(process.pid, max(0, deadline - time.monotonic())):
                raise ProviderError('timeout')
        finally:
            guard['armed'] = False
            # This group was created for this invocation; never use a global CLI kill.
            # Signal it before reaping the leader so the group ID cannot be reused yet.
            _signal_group(process.pid, signal.SIGTERM)
            _exited(process.pid, .5)
            _signal_group(process.pid, signal.SIGKILL)
            process.wait()
            selector.close()
            process.stdout.close()
            process.stderr.close()
        return process.returncode, bytes(buffers['stdout']), bytes(buffers['stderr'])


def _error_reason(text):
    text = text.lower()
    if any(word in text for word in ('rate_limit', 'rate limit', 'usage limit', 'quota')):
        return 'rate_limit'
    if any(word in text for word in ('not logged', 'authentication', 'unauthorized', 'login required', 'invalid api key')):
        return 'authentication_required'
    return 'provider_failed'


def _usage(value):
    if not isinstance(value, dict):
        return None
    keys = ('input_tokens', 'output_tokens')
    if not all(type(value.get(k)) is int and value[k] >= 0 for k in keys):
        return None
    result = {k: value[k] for k in keys}
    for key in ('cached_input_tokens', 'cache_read_input_tokens', 'cache_creation_input_tokens'):
        if type(value.get(key)) is int and value[key] >= 0:
            result[key] = value[key]
    return result


def _model_id(provider, value):
    # Named full IDs are explicit selectors; short/latest aliases need reported identity.
    if provider == 'claude':
        return bool(re.fullmatch(r'claude-[a-z0-9][a-z0-9.-]+', value)) and 'latest' not in value
    return bool(re.fullmatch(r'(?:gpt|o[1-9])-[a-z0-9][a-z0-9.-]+', value)) and 'latest' not in value


class CLIProvider:
    def __init__(self, provider, model, *, frozen=None, allow_untested=False):
        if (provider not in SUPPORTED or not isinstance(model, str) or not model.strip()
                or len(model) > 200 or model.startswith('-')):
            raise ProviderError('provider_and_explicit_model_required')
        # Codex output carries no model identity, so an alias can never be verified later.
        if provider == 'codex' and not _model_id(provider, model):
            raise ProviderError('unresolved_model_alias')
        self.provider, self.model, self.frozen = provider, model, frozen
        self.allow_untested = allow_untested
        self.profile = None

    def _probe(self, argv, *, include_stderr=False, timeout=15):
        with tempfile.TemporaryDirectory(prefix='skill-evolver-probe-') as cwd:
            code, out, err = bounded_process(argv, cwd=cwd, env=login_environment(), timeout=timeout)
        if code:
            raise ProviderError(_error_reason(err.decode('utf-8', 'replace')))
        return (out + (err if include_stderr else b'')).decode('utf-8')

    def inspect(self):
        executable = shutil.which(self.provider)
        if not executable:
            raise ProviderError('cli_not_installed:' + self.provider)
        executable = str(Path(executable).resolve())
        version_output = self._probe([executable, '--version'])
        found = re.search(r'\b\d+\.\d+\.\d+\b', version_output)
        if not found:
            raise ProviderError('unsupported_cli_version:' + self.provider)
        version = found.group()
        tested = version == SUPPORTED[self.provider]
        if not tested and not self.allow_untested:
            raise ProviderError('unsupported_cli_version:' + self.provider)
        help_args = ['exec', '--help'] if self.provider == 'codex' else ['--help']
        help_text = self._probe([executable, *help_args])
        required = (['--ignore-user-config', '--ignore-rules', '--ephemeral', '--output-schema', '--json', '--sandbox']
                    if self.provider == 'codex' else
                    ['--safe-mode', '--restricted', '--tools', '--strict-mcp-config',
                     '--json-schema', '--output-format', '--no-session-persistence'])
        if not all(flag in help_text for flag in required):
            raise ProviderError('unsupported_cli_capability')
        if self.provider == 'claude':
            status = load_json(self._probe([executable, 'auth', 'status', '--json']))
            if not isinstance(status, dict) or status.get('loggedIn') is not True or status.get('authMethod') not in ('claude.ai', 'oauth'):
                raise ProviderError('subscription_login_required:claude')
        else:
            status = self._probe([executable, 'login', 'status'], include_stderr=True)
            if 'chatgpt' not in status.lower() or 'logged in' not in status.lower():
                raise ProviderError('subscription_login_required:codex')
        self.profile = {'provider': self.provider, 'model': self.model, 'executable': executable,
                        'cli_version': version, 'profile_version': PROFILE_VERSION,
                        'argv': self._control_argv(executable), 'env_policy_version': 1}
        if not tested:
            # Recorded only for opted-in versions so existing frozen profiles still match.
            self.profile['version_tested'] = False
        if self.frozen is not None and self.profile != self.frozen:
            raise ProviderError('provider_profile_changed')
        return self.profile

    def _control_argv(self, executable):
        if self.provider == 'claude':
            return [executable, '--safe-mode', '--restricted', '-p', '--model', self.model,
                    '--tools', '', '--strict-mcp-config',
                    '--mcp-config', '{"mcpServers":{}}', '--no-session-persistence',
                    '--output-format', 'stream-json', '--verbose', '--json-schema', SCHEMA_SLOT]
        return [executable, 'exec', '--ignore-user-config', '--ignore-rules', '--ephemeral',
                '--skip-git-repo-check', '--sandbox', 'read-only', '--model', self.model,
                '-c', 'approval_policy="never"', '-c', 'features.shell_tool=false',
                '-c', 'features.hooks=false', '-c', 'features.apps=false',
                '-c', 'features.plugins=false', '-c', 'skills.include_instructions=false',
                '-c', 'features.skill_search=false', '-c', 'features.skill_mcp_dependency_install=false',
                '-c', 'features.in_app_browser=false', '-c', 'features.in_app_local_automation=false',
                '-c', 'project_doc_max_bytes=0', '-c', 'skills.bundled.enabled=false',
                '-c', 'suppress_unstable_features_warning=true',
                '-c', 'features.multi_agent=false', '-c', 'web_search="disabled"',
                '--output-schema', SCHEMA_SLOT, '--json', '-']

    def argv(self, schema_path, schema):
        """Execute the frozen profile argv; only the schema slot varies per call."""
        if self.profile is None:
            raise ProviderError('provider_not_inspected')
        frozen = self.profile['argv']
        if frozen.count(SCHEMA_SLOT) != 1:
            raise ProviderError('provider_profile_changed')
        value = json.dumps(schema, separators=(',', ':')) if self.provider == 'claude' else str(schema_path)
        return [value if part == SCHEMA_SLOT else part for part in frozen]

    def complete(self, context, schema, *, timeout=120, stream_limit=1024 * 1024, prompt_limit=256 * 1024):
        started = time.monotonic()
        # Skill contents are JSON data, never an explicit `$skill` CLI invocation.
        encoded = json.dumps(context, ensure_ascii=False, separators=(',', ':')).replace('$', '\\u0024').encode()
        if len(encoded) > prompt_limit:
            raise ProviderError('prompt_limit')
        if self.profile is None:
            self.inspect()
        # A binary update after prepare must not silently change the comparison.
        observed_version = re.search(r'\b\d+\.\d+\.\d+\b', self._probe(
            [self.profile['executable'], '--version'], timeout=min(15, timeout)))
        if not observed_version or observed_version.group() != self.profile['cli_version']:
            raise ProviderError('provider_profile_changed')
        with tempfile.TemporaryDirectory(prefix='skill-evolver-call-') as cwd:
            schema_path = Path(cwd) / 'response-schema.json'
            schema_path.write_text(json.dumps(schema))
            remaining = timeout - (time.monotonic() - started)
            if remaining <= 0:
                raise ProviderError('timeout')
            code, out, err = bounded_process(self.argv(schema_path, schema), cwd=cwd,
                                            env=login_environment(), input_bytes=encoded,
                                            timeout=remaining, limit=stream_limit)
        if code:
            raise ProviderError(_error_reason(err.decode('utf-8', 'replace') + out.decode('utf-8', 'replace')))
        try:
            events = [load_json(line) for line in out.decode('utf-8').splitlines() if line.strip()]
            if not events or not all(isinstance(event, dict) for event in events):
                raise ValueError('missing events')
            content, usage, model = self._parse(events)
        except ProviderError:
            raise
        except (ValueError, KeyError, TypeError) as exc:
            raise ProviderError('invalid_provider_output') from exc
        if model is None and not _model_id(self.provider, self.model):
            raise ProviderError('unresolved_model_alias')
        return {'content': content, 'provider': self.provider, 'requested_model': self.model,
                'reported_model': model, 'cli_version': self.profile['cli_version'],
                'model_identity': 'reported' if model else 'requested_only',
                'observed_usage': usage, 'status': 'success',
                'duration_seconds': round(time.monotonic() - started, 4), 'stdout_bytes': len(out)}

    def _parse(self, events):
        if self.provider == 'claude':
            results, inits, models, usage_models = [], [], set(), set()
            for event in events:
                kind = event.get('type')
                if kind not in ('system', 'assistant', 'user', 'result', 'rate_limit_event'):
                    raise ValueError('unknown Claude event')
                if kind == 'system' and event.get('subtype') == 'init':
                    inits.append(event)
                if kind == 'assistant':
                    message = event.get('message', {})
                    if not isinstance(message, dict) or not isinstance(message.get('content', []), list):
                        raise ValueError('invalid assistant message')
                    if isinstance(message.get('model'), str):
                        models.add(message['model'])
                    for block in message.get('content', []):
                        if not isinstance(block, dict):
                            raise ValueError('invalid assistant content')
                        if block.get('type') == 'tool_use' and block.get('name') != 'StructuredOutput':
                            raise ProviderError('unexpected_tool_activity')
                if kind == 'result':
                    results.append(event)
            # The tool inventory must be reported; its absence is not evidence of no tools.
            tools = inits[0].get('tools') if len(inits) == 1 else None
            servers = inits[0].get('mcp_servers') if len(inits) == 1 else None
            if (not isinstance(tools, list) or not all(isinstance(tool, str) for tool in tools)
                    or not isinstance(servers, list)):
                raise ProviderError('unverified_tool_inventory')
            if set(tools) - {'StructuredOutput'} or servers:
                raise ProviderError('unexpected_available_tools')
            if isinstance(inits[0].get('model'), str):
                models.add(inits[0]['model'])
            if len(results) != 1:
                raise ValueError('missing result')
            result = results[0]
            if result.get('is_error') or result.get('subtype') != 'success':
                raise ProviderError(_error_reason(json.dumps(result)))
            content = result['structured_output']
            model_usage = result.get('modelUsage', {})
            if isinstance(model_usage, dict):
                usage_models.update(model_usage)
            if not models and len(usage_models) == 1:
                models.update(usage_models)
            if len(models) > 1:
                raise ProviderError('ambiguous_reported_model')
            model = next(iter(models), None)
            return content, _usage(result.get('usage')), model
        texts, ends, model = [], [], None
        for event in events:
            kind = event.get('type')
            if not isinstance(kind, str):
                raise ValueError('missing event type')
            if kind in ('error', 'turn.failed'):
                raise ProviderError(_error_reason(json.dumps(event)))
            if kind.startswith('item.'):
                item = event.get('item', {})
                if not isinstance(item, dict):
                    raise ValueError('invalid item')
                if item.get('type') == 'error':
                    raise ProviderError(_error_reason(str(item.get('message', ''))))
                if item.get('type') not in ('agent_message', 'reasoning'):
                    raise ProviderError('unexpected_tool_activity')
                if kind == 'item.completed' and item.get('type') == 'agent_message':
                    texts.append(item['text'])
            elif kind not in ('thread.started', 'turn.started', 'turn.completed'):
                raise ValueError('unknown event')
            if kind == 'turn.completed':
                ends.append(event)
            if isinstance(event.get('model'), str):
                model = event['model']
        if len(ends) != 1 or not texts:
            raise ValueError('missing completed turn')
        return load_json(texts[-1]), _usage(ends[0].get('usage')), model
