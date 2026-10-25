"""Single-writer, fsynced journal and atomic snapshots."""
import json
import os
from pathlib import Path
from contextlib import contextmanager
import fcntl


class Store:
    def __init__(self, path):
        self.path = Path(path)
        self.path.mkdir(parents=True, exist_ok=True)
        self.journal = self.path / 'journal.jsonl'

    @contextmanager
    def lock(self):
        with (self.path / '.lock').open('a') as handle:
            try:
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except BlockingIOError as exc:
                raise ValueError('experiment already has an active writer') from exc
            try:
                yield
            finally:
                fcntl.flock(handle, fcntl.LOCK_UN)

    def events(self):
        if not self.journal.exists():
            return []
        data = self.journal.read_bytes()
        lines = data.splitlines(keepends=True)
        events = []
        for index, line in enumerate(lines):
            if not line.endswith(b'\n') and index == len(lines) - 1:
                # A torn final write never constitutes a committed event.
                break
            events.append(json.loads(line))
        return events

    def append(self, event):
        # Discard only a torn, uncommitted suffix before the next atomic record.
        if self.journal.exists():
            data = self.journal.read_bytes()
            if data and not data.endswith(b'\n'):
                with self.journal.open('r+b') as handle:
                    handle.truncate(data.rfind(b'\n') + 1)
                    handle.flush()
                    os.fsync(handle.fileno())
        with self.journal.open('ab') as handle:
            handle.write((json.dumps(event, sort_keys=True) + '\n').encode())
            handle.flush()
            os.fsync(handle.fileno())

    def write(self, name, value):
        target = self.path / name
        target.parent.mkdir(parents=True, exist_ok=True)
        temporary = target.with_suffix(target.suffix + '.tmp')
        with temporary.open('w') as handle:
            json.dump(value, handle, indent=2, sort_keys=True)
            handle.write('\n')
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, target)
        directory = os.open(target.parent, os.O_RDONLY)
        try:
            os.fsync(directory)
        finally:
            os.close(directory)


def latest_trials(events):
    """Reduce retries by planned identity, retaining the highest attempt."""
    latest: dict[str, tuple[int, dict]] = {}
    for event in events:
        if event['kind'] != 'trial':
            continue
        planned, _, suffix = event['id'].rpartition('/')
        attempt = event.get('attempt', int(suffix) if suffix.isdigit() else 0)
        previous = latest.get(planned)
        if previous is None or attempt >= previous[0]:
            latest[planned] = (attempt, event)
    return [value[1] for value in latest.values()]


def event_scope(event):
    """Read historical scope fields without trusting completion summaries."""
    scope = event.get('scope')
    if isinstance(scope, str):
        return scope
    if all(key in event for key in ('method','search','phase')):
        return f'{event["method"]}/{event["search"]}/{event["phase"]}'
    parts = event.get('id', '').split('/')
    if parts[0] == 'proposal' and len(parts) >= 4:
        return f'{parts[1]}/{parts[2]}/search'
    return '/'.join(parts[:3])


def search_validity(events, method, search):
    scope = f'{method}/{search}/search'
    relevant = [event for event in events if event_scope(event) == scope]
    reasons = set()
    for trial in latest_trials(relevant):
        if trial.get('status') in ('infrastructure_error','indeterminate'):
            reasons.add('unresolved_search_trial:' + trial['id'])
    # Unknown execution cannot be retroactively cleared by an unrelated retry.
    for event in relevant:
        if event['kind'] in ('trial','proposal_result') and event.get('status') == 'indeterminate':
            reasons.add('indeterminate_execution:' + event['id'])
        if event['kind'] == 'trial' and event.get('tokens') is None:
            reasons.add('unknown_trial_usage:' + event['id'])
        if event['kind'] == 'proposal_result' and event.get('tokens') is None:
            reasons.add('unknown_proposal_usage:' + event['id'])
    results = {event['id'] for event in relevant if event['kind'] in ('trial','proposal','proposal_result')}
    for event in relevant:
        if event['kind'] == 'reserve' and event['id'] not in results:
            reasons.add('unresolved_search_reservation:' + event['id'])
    return {'valid':not reasons,'reasons':sorted(reasons)}


def runtime_validity(events, manifest):
    reasons = set()
    if not manifest.get('runtime_image_id'):
        reasons.add('runtime_image_not_frozen')
    expected = manifest.get('environment_hash')
    if not expected:
        reasons.add('environment_hash_not_frozen')
    for event in events:
        if event['kind'] != 'trial':
            continue
        observed = event.get('environment_hash')
        if observed and expected and observed != expected:
            reasons.add('mixed_environment_hashes')
        if event.get('status') in ('success','task_failure','timeout') and not observed:
            reasons.add('trial_environment_hash_missing')
    return {'valid':not reasons,'reasons':sorted(reasons)}


def method_validity(events, manifest, method, search):
    search_state = search_validity(events, method, search)
    runtime_state = runtime_validity(events, manifest)
    reasons = sorted(set(search_state['reasons'] + runtime_state['reasons']))
    return {'valid':not reasons,'reasons':reasons}
