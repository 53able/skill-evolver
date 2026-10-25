import io
import os
import tarfile
import tempfile
import json
import subprocess
import signal
import time
from pathlib import Path
from unittest.mock import patch
import unittest

from skill_evolver.sandbox import ArtifactError, DockerSandbox, SandboxError, collect_tar, relative_path, recover_allocation


def config():
    return {'image': 'alpine:3', 'timeout_seconds': 5, 'max_file_bytes': 4096,
            'max_total_bytes': 16384, 'max_files': 10}


def archive(name='result.txt', data=b'ok', kind=tarfile.REGTYPE, link=''):
    stream = io.BytesIO()
    with tarfile.open(fileobj=stream, mode='w') as tar:
        info = tarfile.TarInfo(name)
        info.type = kind
        info.linkname = link
        info.size = len(data) if kind == tarfile.REGTYPE else 0
        tar.addfile(info, io.BytesIO(data))
    stream.seek(0)
    return stream


class CollectorTests(unittest.TestCase):
    def collect(self, stream, **changes):
        limits = config()
        limits.pop('image')
        limits.pop('timeout_seconds')
        limits.update(changes)
        return collect_tar(stream, ['result.txt'], **limits)

    def test_regular_detached_text(self):
        stream = archive()
        output = self.collect(stream)
        stream.close()
        self.assertEqual(output, {'result.txt': 'ok'})

    def test_malicious_members(self):
        for name, kind in [('../result.txt', tarfile.REGTYPE), ('/result.txt', tarfile.REGTYPE),
                           ('result.txt', tarfile.SYMTYPE), ('result.txt', tarfile.LNKTYPE),
                           ('result.txt', tarfile.FIFOTYPE), ('result.txt', tarfile.CHRTYPE)]:
            with self.subTest(name=name, kind=kind), self.assertRaises(ArtifactError):
                self.collect(archive(name, kind=kind, link='/etc/passwd' if kind in (tarfile.SYMTYPE, tarfile.LNKTYPE) else ''))

    def test_limits_and_invalid_archive(self):
        for change in ({'max_file_bytes': 1}, {'max_total_bytes': 1}, {'max_files': 0}):
            with self.assertRaises(ArtifactError):
                self.collect(archive(), **change)
        with self.assertRaises(ArtifactError):
            self.collect(io.BytesIO(b'garbage'))
        with self.assertRaises(ArtifactError):
            self.collect(archive('other.txt'))

    def test_reject_unsafe_input(self):
        for path in ('../x', '/x', 'a/../x', 'a//b', './x', 'a\\b', ''):
            with self.assertRaises(ValueError):
                relative_path(path)

    def test_zero_runtime_bounds_rejected_before_allocation(self):
        for field in ('max_files', 'max_total_bytes', 'max_file_bytes'):
            c = config()
            c[field] = 0
            with self.assertRaises(ValueError):
                DockerSandbox(c, '', {})

    def test_collect_requires_stop(self):
        with self.assertRaises(SandboxError):
            DockerSandbox(config(), '', {}).collect([])


@unittest.skipUnless(os.environ.get('SKILL_EVOLVER_DOCKER_TESTS') == '1', 'Docker integration opt-in')
class DockerTests(unittest.TestCase):
    def setUp(self):
        self.sandbox = DockerSandbox(config(), 'secret-free skill', {'input.txt': 'Hello'})
        with patch.dict(os.environ, {'OPENAI_API_KEY': 'host-only-boundary-marker'}):
            self.sandbox.start()
        self.addCleanup(self.sandbox.close)

    def test_real_tools_boundary_flags_and_collection(self):
        s = self.sandbox
        self.assertEqual(s.tool('read_file', {'path': '/SKILL.md'}), 'secret-free skill')
        self.assertIn('/input/input.txt', s.tool('list_files', {'path': '/input'}))
        s.tool('write_file', {'path': '/output/result.txt', 'content': 'stable'})
        probe = s.tool('run_command', {'command': 'test ! -e /var/run/docker.sock && test ! -e /host && test ! -e /input/expected && test -z "$OPENAI_API_KEY" && test ! -w /input/input.txt && test ! -w /SKILL.md && test "$(id -u)" = 65534 && echo boundary-ok'})
        self.assertIn('boundary-ok', probe)
        with tempfile.NamedTemporaryFile(prefix='host-answer-') as answer:
            answer.write(b'host-only-answer')
            answer.flush()
            self.assertEqual(s.tool('run_command', {'command': 'test ! -e ' + answer.name + ' && echo host-hidden'}).strip(), 'host-hidden')
        self.assertIn('lo', s.tool('run_command', {'command': 'ls /sys/class/net'}))
        self.assertNotIn('eth0', s.tool('run_command', {'command': 'ls /sys/class/net'}))
        inspected = json.loads(s._docker(['inspect', s.name]))[0]
        host = inspected['HostConfig']
        self.assertEqual(host['NetworkMode'], 'none')
        self.assertTrue(host['ReadonlyRootfs'])
        self.assertEqual(host['CapDrop'], ['ALL'])
        self.assertIn('no-new-privileges', host['SecurityOpt'])
        self.assertEqual(host['PidsLimit'], 64)
        self.assertGreater(host['Memory'], 0)
        self.assertGreater(host['NanoCpus'], 0)
        self.assertEqual(inspected['Config']['User'], '0:0')
        self.assertEqual(inspected['Config']['Entrypoint'], ['/bin/sleep'])
        self.assertEqual(s.tool('run_command', {'command': 'kill -TERM 1 2>/dev/null; test $? -ne 0 && echo protected'}).strip(), 'protected')
        s.stop()
        self.assertEqual(s.collect(['result.txt']), {'result.txt': 'stable'})

    def test_timeout_descendants_and_fresh_state(self):
        s = self.sandbox
        s.deadline = __import__('time').monotonic() + .2
        with self.assertRaises(TimeoutError):
            s.tool('run_command', {'command': 'sleep 30 & wait'})
        s.stop()
        self.assertEqual(s._docker(['inspect', '--format', '{{.State.Running}}', s.name]).strip(), b'false')
        other = DockerSandbox(config(), '', {})
        try:
            other.start()
            self.assertEqual(other.tool('list_files', {'path': '/output'}), '')
            other.stop()
        finally:
            other.close()

    def test_symlink_artifact_rejected(self):
        s = self.sandbox
        s.tool('run_command', {'command': 'ln -s /etc/passwd /output/result.txt'})
        s.stop()
        with self.assertRaises(ArtifactError):
            s.collect(['result.txt'])


@unittest.skipUnless(os.environ.get('SKILL_EVOLVER_DOCKER_TESTS') == '1', 'Docker integration opt-in')
class RuntimeBoundTests(unittest.TestCase):
    def test_runtime_bytes_inodes_and_stopped_snapshot(self):
        c = config()
        c.update(max_total_bytes=4096, max_file_bytes=4096, max_files=3)
        s = DockerSandbox(c, 'skill', {})
        try:
            s.start()
            result = s.tool('run_command', {'command': 'dd if=/dev/zero of=/output/big bs=4096 count=2 2>/tmp/dd-log; rc=$?; test "$rc" -ne 0 && test "$(wc -c < /output/big)" -le 4096 && echo byte-bound'})
            self.assertIn('byte-bound', result)
            result = s.tool('run_command', {'command': 'rm /output/big; touch /output/a /output/b /output/c; touch /output/d 2>/tmp/inode-log; test $? -ne 0 && echo inode-bound'})
            self.assertIn('inode-bound', result)
            s.tool('run_command', {'command': 'rm /output/a /output/b /output/c; printf stable > /output/result.txt'})
            s.stop()
            s.stop()
            self.assertEqual(s.collect(['result.txt']), {'result.txt': 'stable'})
        finally:
            s.close()

    def test_recorder_precedes_creation_and_recovery_refuses_wrong_labels(self):
        events = []
        def record(event):
            self.assertFalse(Path(event['resources']['staging']['path']).exists())
            events.append(event)
        s = DockerSandbox(config(), '', {}, trial_id='logical', resource_recorder=record)
        try:
            s.start()
            self.assertEqual(events[0]['trial_id'], 'logical')
            forged = json.loads(json.dumps(events[0]))
            forged['resources']['volume'] = 'unrelated-volume'
            with self.assertRaises(SandboxError):
                recover_allocation(forged)
            self.assertEqual(s._docker(['inspect', '--format', '{{.State.Running}}', s.name]).strip(), b'true')
        finally:
            s.resource_recorder = None
            s.close()

    def test_naturally_expired_main_stop_and_collect(self):
        c = config()
        c['timeout_seconds'] = 1
        s = DockerSandbox(c, '', {})
        try:
            s.start()
            s.tool('write_file', {'path': '/output/result.txt', 'content': 'survives-stop'})
            time.sleep(1.1)
            s.stop()
            s.stop()
            self.assertEqual(s.collect(['result.txt']), {'result.txt': 'survives-stop'})
        finally:
            s.close()

    def test_lost_create_response_cleanup_and_resource_events(self):
        events = []
        s = DockerSandbox(config(), '', {}, trial_id='lost-response', resource_recorder=events.append)
        original = s._docker
        def lose_create_response(args, **kwargs):
            result = original(args, **kwargs)
            if args[0] == 'create':
                raise SandboxError('lost create response')
            return result
        try:
            with patch.object(s, '_docker', side_effect=lose_create_response):
                with self.assertRaises(SandboxError):
                    s.start()
            self.assertFalse(s.created)
        finally:
            s.close()
        self.assertEqual([e['kind'] for e in events], ['resource_allocated', 'resource_released'])
        for args in (['inspect', s.name], ['inspect', s.name + '-keeper'], ['volume', 'inspect', s.name]):
            with self.assertRaises(SandboxError):
                original(args)
        self.assertFalse(s.staging.exists())

    def test_actual_nonce_label_mismatch_fails_closed(self):
        s = DockerSandbox(config(), '', {})
        try:
            s._docker(['volume', 'create', '--label', 'skill-evolver.nonce=wrong', s.name])
            with self.assertRaises(SandboxError):
                recover_allocation(s.event)
            # Only this deliberately created exact test volume is inspected/removed.
            self.assertIn(b'wrong', s._docker(['volume', 'inspect', s.name]))
        finally:
            s._docker(['volume', 'rm', s.name])

    def test_owner_sigkill_daemon_lifetimes_and_exact_recovery(self):
        with tempfile.TemporaryDirectory() as directory:
            journal = Path(directory) / 'events.jsonl'
            script = r"""
import json, os, sys
import skill_evolver.sandbox as module
module.COLLECTION_GRACE_SECONDS = 1
from skill_evolver.sandbox import DockerSandbox
c = json.loads(sys.argv[2])
def record(event):
    with open(sys.argv[1], 'a') as stream:
        stream.write(json.dumps(event) + '\n')
        stream.flush()
        os.fsync(stream.fileno())
s = DockerSandbox(c, 'skill', {}, trial_id='killed-owner', resource_recorder=record)
s.start()
s.tool('run_command', {'command':'sleep 30 </dev/null >/dev/null 2>&1 & echo child-started'})
print(json.dumps({'pid':os.getpid(),'event':s.event}), flush=True)
import time
time.sleep(60)
"""
            c = config()
            c['timeout_seconds'] = 3
            owner = subprocess.Popen(['uv', 'run', '--no-project', 'python', '-c', script, str(journal), json.dumps(c)], stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            event = None
            try:
                assert owner.stdout is not None
                line = owner.stdout.readline()
                if not line:
                    _, stderr = owner.communicate(timeout=10)
                    self.fail('owner failed before ready: ' + stderr)
                identity = json.loads(line)
                event = identity['event']
                os.kill(identity['pid'], signal.SIGKILL)
                owner.communicate(timeout=10)
                time.sleep(4.2)
                probe = DockerSandbox(config(), '', {})
                for key in ('main', 'keeper'):
                    state = json.loads(probe._docker(['inspect', event['resources'][key]]))[0]['State']
                    self.assertFalse(state['Running'])
                    self.assertEqual(state['Pid'], 0)
                from skill_evolver.runner import TrialRunner
                from test_runner import config as runner_config
                runner = TrialRunner(runner_config())
                released = []
                runner.resource_recorder = released.append
                runner.recover_resources([json.loads(line) for line in journal.read_text().splitlines()])
                self.assertEqual(released[0]['kind'], 'resource_released')
                self.assertFalse(Path(event['resources']['staging']['path']).exists())
                for args in (['inspect', event['resources']['main']], ['inspect', event['resources']['keeper']], ['volume', 'inspect', event['resources']['volume']]):
                    with self.assertRaises(SandboxError):
                        probe._docker(args)
            finally:
                if owner.poll() is None:
                    owner.kill()
                owner.communicate(timeout=10)
                if event is None and journal.exists():
                    event = json.loads(journal.read_text().splitlines()[0])
                if event is not None:
                    recover_allocation(event)
