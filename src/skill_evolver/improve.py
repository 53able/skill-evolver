"""Human-reviewed, bounded text-skill improvement; original files are read only."""
import copy
import difflib
import hashlib
import json
import os
from pathlib import Path
import re
import stat
import tempfile
import time
from .cli_provider import CLIProvider, ProviderError, load_json
from .genome import candidate, digest, validate_skill
from .store import Store
from . import improve_eval as ev


KIND = 'skill-improvement'
SCHEMA_VERSION = 1
DEFAULT_LIMITS = {'max_iterations': 3, 'max_calls': 60, 'call_timeout': 120,
                  'run_deadline': 3600, 'prompt_bytes': 256 * 1024, 'stream_bytes': 1024 * 1024}
SKILL_BYTES = 64 * 1024


def fingerprint(value):
    return hashlib.sha256(json.dumps(value, sort_keys=True, separators=(',', ':'),
                                     ensure_ascii=False).encode()).hexdigest()


def _read(path, limit=2 * 1024 * 1024):
    with path.open('rb') as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError('saved artifact exceeds size limit')
    return load_json(data.decode())


def guard_directory(path):
    path = Path(path).absolute()
    if not path.is_dir() or path.is_symlink():
        raise ValueError('improvement directory does not exist or is a symlink')
    if any(p.is_symlink() or (p.is_file() and p.stat().st_nlink > 1) for p in path.rglob('*')):
        raise ValueError('linked files are not allowed in improvement artifacts')
    return path


def write_text(store, name, value):
    """Write only caller-owned fixed names; avoid following pre-existing temp links."""
    guard_directory(store.path)
    target = store.path / name
    target.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(mode='w', encoding='utf-8', dir=target.parent,
                                     prefix='.improve-', delete=False) as handle:
        temporary = Path(handle.name)
        try:
            handle.write(value)
            handle.flush()
            os.fsync(handle.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)
    finally:
        temporary.unlink(missing_ok=True)


class ImproveStore(Store):
    """Store whose writes refuse linked artifacts instead of following them."""

    def write(self, name, value):
        write_text(self, name, json.dumps(value, indent=2, sort_keys=True) + '\n')

    def append(self, event):
        guard_directory(self.path)
        handle = os.fdopen(os.open(self.journal, os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_NOFOLLOW, 0o644), 'a+b')
        with handle:
            info = os.fstat(handle.fileno())
            if not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
                raise ValueError('linked files are not allowed in improvement artifacts')
            # Discard only a torn, uncommitted suffix before the next atomic record.
            handle.seek(0)
            data = handle.read()
            if data and not data.endswith(b'\n'):
                handle.truncate(data.rfind(b'\n') + 1)
            handle.write((json.dumps(event, sort_keys=True) + '\n').encode())
            handle.flush()
            os.fsync(handle.fileno())


def load_bundle(directory):
    directory = guard_directory(directory)
    manifest = _read(directory / 'manifest.json')
    if not isinstance(manifest, dict) or manifest.get('kind') != KIND or manifest.get('schema_version') != SCHEMA_VERSION:
        raise ValueError('not a supported skill-improvement directory')
    if not isinstance(manifest.get('source_path'), str) or not manifest['source_path']:
        raise ValueError('invalid source path')
    original = _read(directory / 'original.json')
    if not isinstance(original, dict) or not isinstance(original.get('text'), str) or digest(original['text']) != manifest.get('original_hash'):
        raise ValueError('original snapshot hash differs')
    limits = manifest.get('limits')
    if not isinstance(limits, dict) or set(limits) != set(DEFAULT_LIMITS):
        raise ValueError('invalid improvement limits')
    if any(type(limits[k]) is not int or not 0 < limits[k] <= upper for k, upper in DEFAULT_LIMITS.items()):
        raise ValueError('improvement limits exceed the supported bounds')
    profile = manifest.get('provider_profile')
    if not isinstance(profile, dict) or profile.get('provider') not in ('claude', 'codex'):
        raise ValueError('invalid provider profile')
    evaluation = ev.validate_evaluation(_read(directory / 'evaluation.json'))
    bundle = {'kind': KIND, 'schema_version': SCHEMA_VERSION,
              'source_path': manifest['source_path'], 'original_hash': manifest['original_hash'],
              'original': original, 'evaluation': evaluation, 'provider_profile': profile,
              'reported_model': manifest.get('reported_model'),
              'model_identity': manifest.get('model_identity'), 'limits': limits}
    return manifest, bundle


def verify_original(bundle):
    try:
        current = Path(bundle['source_path']).read_bytes()
    except OSError as exc:
        raise ValueError('original skill is no longer available') from exc
    if hashlib.sha256(current).hexdigest() != bundle['original_hash']:
        raise ValueError('original skill changed; start a new prepare')


def _save_candidate(store, text, original, *, operation, reason=None):
    record = candidate(text, operation=operation)
    record['diff'] = ''.join(difflib.unified_diff(original.splitlines(True), text.splitlines(True),
                                               fromfile='original/SKILL.md', tofile='candidate/SKILL.md'))
    if reason is not None:
        record['rejection_reason'] = reason
    store.write('candidates/' + record['id'] + '.json', record)
    return record


class Calls:
    def __init__(self, store, provider, limits, *, bundle=None, progress=None):
        self.store, self.provider, self.limits, self.bundle = store, provider, limits, bundle
        self.progress = progress
        self.deadline = time.monotonic() + limits['run_deadline']
        self.reported_model = bundle.get('reported_model') if bundle else None
        self.model_identity = bundle.get('model_identity') if bundle else None

    def complete(self, identity, context, schema):
        events = self.store.events()
        starts = {e['call_id'] for e in events if e['kind'] == 'call_started'}
        if identity in starts:
            raise ProviderError('call_already_recorded')
        if len(starts) >= self.limits['max_calls']:
            raise ProviderError('call_limit')
        remaining = self.deadline - time.monotonic()
        if remaining <= 0:
            raise ProviderError('run_deadline')
        if self.bundle:
            verify_original(self.bundle)
        if self.progress:
            self.progress(len(starts) + 1, self.limits['max_calls'], identity)
        self.store.append({'kind': 'call_started', 'call_id': identity, 'phase': identity.split('/')[0]})
        try:
            result = self.provider.complete(context, schema,
                timeout=min(self.limits['call_timeout'], remaining),
                stream_limit=self.limits['stream_bytes'], prompt_limit=self.limits['prompt_bytes'])
            reported = result['reported_model']
            if self.reported_model is not None and reported != self.reported_model:
                raise ProviderError('reported_model_changed_or_missing')
            if self.model_identity == 'requested_only' and reported is not None:
                # The initial run has no verified alias mapping; don't mix identities.
                raise ProviderError('reported_model_identity_changed')
            if self.model_identity is None:
                self.reported_model, self.model_identity = reported, result['model_identity']
            self.store.append({'kind': 'call_finished', 'call_id': identity, **result})
        except ProviderError as exc:
            self.store.append({'kind': 'call_finished', 'call_id': identity, 'status': exc.reason,
                               'observed_usage': None})
            raise
        if self.bundle:
            verify_original(self.bundle)
        return result['content']


def _failure_reason(exc, expected, reason):
    # Stable codes only: exception text may carry provider output or credentials.
    if isinstance(exc, ProviderError):
        return exc.reason
    if isinstance(exc, KeyboardInterrupt):
        return 'interrupted'
    return reason if isinstance(exc, expected) else 'unexpected_error'


def prepare(skill, provider_name, model, output, *, provider=None, allow_untested=False, progress=None):
    source = Path(skill).resolve(strict=True)
    text = source.read_bytes().decode('utf-8')
    if len(text.encode()) > SKILL_BYTES:
        raise ValueError('M1 supports skill files up to 64 KiB')
    validate_skill(text, text, source.parent)
    if re.search(r'\b(?:scripts|references|assets)/[^\s`)>]+', text):
        raise ValueError('unsupported: companion files require a separate execution scope')
    directory = Path(output).absolute()
    resolved = directory.resolve()
    if resolved == source.parent or resolved.is_relative_to(source.parent) or source.is_relative_to(resolved):
        raise ValueError('output directory must be separate from the source skill directory')
    if directory.exists() or directory.is_symlink():
        raise ValueError('output directory already exists; choose a new directory')
    provider = provider or CLIProvider(provider_name, model, allow_untested=allow_untested)
    profile = provider.inspect()
    directory.mkdir(parents=True)
    store = ImproveStore(directory)
    with store.lock():
        manifest = {'kind': KIND, 'schema_version': SCHEMA_VERSION, 'source_path': str(source),
                    'original_hash': digest(text), 'provider_profile': profile,
                    'limits': copy.deepcopy(DEFAULT_LIMITS), 'state': 'preparing'}
        store.write('manifest.json', manifest)
        try:
            store.write('original.json', candidate(text))
            calls = Calls(store, provider, manifest['limits'], progress=progress)
            evaluation = ev.validate_evaluation(calls.complete('prepare/evaluation', ev.evaluation_context(text), ev.EVALUATION_SCHEMA))
            if hashlib.sha256(source.read_bytes()).hexdigest() != manifest['original_hash']:
                raise ValueError('original skill changed during prepare')
            store.write('evaluation.json', evaluation)
            manifest.update(state='draft' if evaluation['execution_scope'] == 'text_only' else 'unsupported',
                            reported_model=calls.reported_model, model_identity=calls.model_identity)
            store.write('manifest.json', manifest)
        except (Exception, KeyboardInterrupt) as exc:
            manifest.update(state='incomplete', reason=_failure_reason(exc, ValueError, 'invalid_evaluation_or_source'))
            store.write('manifest.json', manifest)
            raise
    from .improve_report import report
    return report(directory)


def _prompts_fit(evaluation, original, proposed, limit):
    # Mirrors CLIProvider.complete encoding so an oversized candidate is rejected before a call is recorded.
    contexts = [ev.purpose_context(evaluation, original, proposed), ev.proposal_context(evaluation, proposed, [])]
    contexts += [ev.execution_context(evaluation, proposed, case) for case in evaluation['development'] + evaluation['heldout']]
    return all(len(json.dumps(c, ensure_ascii=False, separators=(',', ':')).replace('$', '\\u0024').encode()) <= limit
               for c in contexts)


def _trial(calls, evaluation, skill, case, identity):
    response = ev.validate_response(calls.complete(identity + '/execute',
        ev.execution_context(evaluation, skill, case), ev.OUTPUT_SCHEMA), 'output')
    judgment = ev.validate_response(calls.complete(identity + '/judge',
        ev.judgment_context(evaluation, case, response['output']), ev.JUDGMENT_SCHEMA), 'judgment', evaluation)
    row = {'case_id': case['id'], 'output': response['output'], 'criteria': judgment['criteria']}
    calls.store.append({'kind': 'trial', 'trial_id': identity, **row})
    return row


def _score(calls, evaluation, skill, split, identity):
    return [_trial(calls, evaluation, skill, case, identity + '/' + case['id'])
            for case in evaluation[split]]


def run(directory, approval, *, provider=None, progress=None):
    directory = guard_directory(directory)
    store = ImproveStore(directory)
    with store.lock():
        manifest, current = load_bundle(directory)
        current_hash = fingerprint(current)
        if not isinstance(approval, str) or not re.fullmatch(r'[0-9a-f]{64}', approval) or approval != current_hash:
            raise ValueError('approval hash differs; read improve report and confirm the current evaluation')
        verify_original(current)
        if current['evaluation']['execution_scope'] != 'text_only':
            raise ValueError('unsupported: this skill requires a separate execution scope')
        approved_path = directory / 'approved.json'
        if approved_path.exists():
            saved = _read(approved_path)
            if fingerprint(saved) != approval:
                raise ValueError('approved inputs changed; start a new prepare')
            bundle = saved
        else:
            if manifest['state'] != 'draft':
                raise ValueError('run is not a reviewable draft; start a new prepare')
            # Snapshot exactly the bytes represented by the already-checked in-memory value.
            store.write('approved.json', current)
            bundle = current
        if manifest['state'] == 'completed':
            from .improve_report import report_locked
            return report_locked(store)
        events = store.events()
        if manifest['state'] != 'draft' or any(e['kind'] == 'call_started' and e['call_id'] != 'prepare/evaluation' for e in events):
            if manifest['state'] != 'incomplete':
                manifest.update(state='incomplete', reason='prior_execution_not_reissued')
                store.write('manifest.json', manifest)
            from .improve_report import report_locked
            return report_locked(store)
        profile = bundle['provider_profile']
        provider = provider or CLIProvider(profile['provider'], profile['model'], frozen=profile,
                                           allow_untested=profile.get('version_tested') is False)
        if provider.inspect() != bundle['provider_profile']:
            raise ValueError('provider profile changed; start a new prepare')
        calls = Calls(store, provider, bundle['limits'], bundle=bundle, progress=progress)
        manifest.update(state='running')
        store.write('manifest.json', manifest)
        original = bundle['original']['text']
        evaluation = bundle['evaluation']
        try:
            best = _save_candidate(store, original, original, operation='original')
            store.append({'kind': 'approved', 'bundle_hash': approval})
            baseline_dev = _score(calls, evaluation, original, 'development', 'development/original')
            best_dev = baseline_dev
            iterations = bundle['limits']['max_iterations']
            if ev.score(baseline_dev, evaluation)[0] == sum(len(row['criteria']) for row in baseline_dev):
                # Selection needs strictly more passes than a perfect original, so skip proposals.
                store.append({'kind': 'search_stopped', 'reason': 'original_passes_all_development_checks'})
                iterations = 0
            for iteration in range(1, iterations + 1):
                prefix = 'iteration/' + str(iteration)
                content = calls.complete(prefix + '/proposal',
                    ev.proposal_context(evaluation, best['text'], best_dev), ev.PROPOSAL_SCHEMA)
                proposed = content.get('skill_text') if isinstance(content, dict) else None
                try:
                    ev.validate_response(content, 'proposal')
                    if len(proposed.encode()) > SKILL_BYTES:
                        raise ValueError('candidate exceeds 64 KiB')
                    validate_skill(proposed, original, Path(bundle['source_path']).parent)
                    if re.search(r'\b(?:scripts|references|assets)/[^\s`)>]+', proposed):
                        raise ValueError('companion files')
                    if not _prompts_fit(evaluation, original, proposed, bundle['limits']['prompt_bytes']):
                        raise ValueError('candidate prompts exceed the prompt limit')
                except ValueError:
                    if isinstance(proposed, str):
                        _save_candidate(store, proposed, original, operation='rejected', reason='invalid_candidate')
                    store.append({'kind': 'candidate_rejected', 'iteration': iteration, 'reason': 'invalid_candidate'})
                    continue
                record = _save_candidate(store, proposed, original, operation='revision')
                purpose = ev.validate_response(calls.complete(prefix + '/purpose',
                    ev.purpose_context(evaluation, original, proposed), ev.PURPOSE_SCHEMA), 'purpose')
                store.append({'kind': 'purpose_check', 'iteration': iteration, 'candidate_id': record['id'], **purpose})
                if not purpose['preserved']:
                    store.append({'kind': 'candidate_rejected', 'iteration': iteration, 'reason': 'purpose_changed'})
                    continue
                rows = _score(calls, evaluation, proposed, 'development', prefix + '/development')
                selected = ev.better(rows, best_dev, evaluation)
                store.append({'kind': 'candidate_selection', 'iteration': iteration, 'candidate_id': record['id'],
                              'selected': selected})
                if selected:
                    best, best_dev = record, rows
            store.append({'kind': 'best_frozen', 'candidate_id': best['id']})
            original_final = _score(calls, evaluation, original, 'heldout', 'heldout/original')
            best_final = (original_final if best['id'] == digest(original) else
                          _score(calls, evaluation, best['text'], 'heldout', 'heldout/best'))
            improved = best['id'] != digest(original) and ev.better(best_final, original_final, evaluation)
            recommended = best if improved else candidate(original)
            comparison = {'kind': KIND, 'schema_version': SCHEMA_VERSION, 'complete': True,
                          'status': 'observed_improvement' if improved else 'no_observed_improvement',
                          'recommended_id': recommended['id'], 'search_best_id': best['id'],
                          'baseline_development': baseline_dev, 'best_development': best_dev,
                          'original_heldout': original_final, 'best_heldout': best_final,
                          'approval_hash': approval}
            verify_original(bundle)
            store.write('comparison.json', comparison)
            write_text(store, 'candidate/SKILL.md', recommended['text'])
            manifest.update(state='completed')
            store.write('manifest.json', manifest)
        except (Exception, KeyboardInterrupt) as exc:
            reason = _failure_reason(exc, (ValueError, OSError), 'invalid_or_changed_input')
            manifest.update(state='incomplete', reason=reason)
            store.write('manifest.json', manifest)
            store.write('comparison.json', {'kind': KIND, 'schema_version': SCHEMA_VERSION,
                        'complete': False, 'status': 'incomplete', 'reason': reason,
                        'recommended_id': digest(original), 'approval_hash': approval})
            if reason == 'unexpected_error':
                raise
        from .improve_report import report_locked
        return report_locked(store)
