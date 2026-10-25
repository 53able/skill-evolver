"""Docker-only tools and bounded, stopped-container artifact collection."""
from __future__ import annotations

import hashlib
import io
import json
import os
import math
import re
import shutil
from pathlib import Path, PurePosixPath
import selectors
import subprocess
import tarfile
import tempfile
import time
import uuid


class SandboxError(RuntimeError):
    pass


class ArtifactError(SandboxError):
    pass


def relative_path(value: str) -> str:
    if not isinstance(value, str) or not value or '\\' in value or '\x00' in value:
        raise ValueError('invalid relative path')
    path = PurePosixPath(value)
    if path.is_absolute() or any(p in ('..', '.') for p in value.split('/')) or str(path) != value:
        raise ValueError('invalid relative path')
    return value


def collect_tar(stream, output_paths, *, max_file_bytes, max_total_bytes, max_files):
    """Validate every member; return detached text, never extract onto the host."""
    allowed = {relative_path(p) for p in output_paths}
    outputs = {}
    count = total = 0
    seen = set()
    try:
        with tarfile.open(fileobj=stream, mode='r|') as archive:
            for member in archive:
                # docker cp /output/. prefixes members with './'.
                name = member.name
                if name in ('.', './') and member.isdir():
                    continue
                if name.startswith('./'):
                    name = name[2:]
                try:
                    relative_path(name)
                except ValueError as exc:
                    raise ArtifactError('unsafe artifact path') from exc
                if name in seen:
                    raise ArtifactError('duplicate artifact path')
                seen.add(name)
                if member.isdir():
                    continue
                if not member.isreg() or member.linkname or member.sparse is not None:
                    raise ArtifactError('non-regular artifact')
                count += 1
                total += member.size
                if member.size < 0 or member.size > max_file_bytes or total > max_total_bytes or count > max_files:
                    raise ArtifactError('artifact limit exceeded')
                if name not in allowed:
                    raise ArtifactError('unexpected artifact')
                source = archive.extractfile(member)
                if source is None:
                    raise ArtifactError('unreadable regular artifact')
                data = source.read(max_file_bytes + 1)
                if len(data) != member.size:
                    raise ArtifactError('truncated artifact')
                try:
                    outputs[name] = data.decode('utf-8')
                except UnicodeDecodeError as exc:
                    raise ArtifactError('artifact is not UTF-8') from exc
    except (tarfile.TarError, OSError) as exc:
        raise ArtifactError('invalid artifact archive') from exc
    if set(outputs) != allowed:
        raise ArtifactError('missing artifact')
    return outputs


def _bounded_process(args, *, timeout, limit, input_bytes=None):
    """Bound subprocess output in memory and wall time (including docker cp)."""
    with tempfile.TemporaryFile() as stdin:
        if input_bytes is not None:
            stdin.write(input_bytes)
        stdin.seek(0)
        process = subprocess.Popen(args, stdin=stdin, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        assert process.stdout is not None and process.stderr is not None
        selector = selectors.DefaultSelector()
        buffers = {process.stdout.fileno(): bytearray(), process.stderr.fileno(): bytearray()}
        for pipe in buffers:
            selector.register(pipe, selectors.EVENT_READ)
        deadline = time.monotonic() + timeout
        try:
            while selector.get_map():
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Docker operation timed out')
                for key, _ in selector.select(min(remaining, .1)):
                    chunk = os.read(key.fd, 65536)
                    if not chunk:
                        selector.unregister(key.fd)
                    else:
                        buffers[key.fd].extend(chunk)
                        if sum(map(len, buffers.values())) > limit:
                            raise SandboxError('Docker output limit exceeded')
            process.wait(timeout=max(.001, deadline - time.monotonic()))
            if process.returncode:
                raise SandboxError('Docker operation failed: ' + buffers[process.stderr.fileno()].decode('utf-8', 'replace')[:1000])
            return bytes(buffers[process.stdout.fileno()])
        finally:
            if process.poll() is None:
                process.kill()
            process.wait()
            selector.close()
            process.stdout.close()
            process.stderr.close()


LABEL = 'skill-evolver.nonce'
COLLECTION_GRACE_SECONDS = 60


def resolve_image(image):
    resolved = _bounded_process(['docker', 'image', 'inspect', image, '--format', '{{.Id}}'],
                                timeout=15, limit=1024).decode().strip()
    if not re.fullmatch(r'sha256:[0-9a-f]{64}', resolved):
        raise SandboxError('image did not resolve to immutable ID')
    if image.startswith('sha256:') and image != resolved:
        raise SandboxError('pinned image mismatch')
    return resolved


def recover_allocation(event):
    """Recover exact journaled names only after proving nonce labels/ownership."""
    nonce = event.get('resource_id')
    if not isinstance(nonce, str) or not re.fullmatch(r'[0-9a-f]{32}', nonce):
        raise SandboxError('invalid resource identity')
    name = 'skill-evolver-' + nonce
    root = Path(tempfile.gettempdir()) / name
    expected = {'main': name, 'keeper': name + '-keeper', 'volume': name,
                'labels': {LABEL: nonce},
                'staging': {'path': str(root), 'nonce': nonce, 'uid': os.getuid()}}
    if event.get('resources') != expected:
        raise SandboxError('resource ownership record mismatch')
    def docker(args):
        return _bounded_process(['docker', *args], timeout=15, limit=1024 * 1024)
    present = []
    for kind, resource in (('container', name), ('container', name + '-keeper'), ('volume', name)):
        try:
            inspected = json.loads(docker([kind, 'inspect', resource]))[0]
        except SandboxError as exc:
            if 'No such' in str(exc) or 'no such' in str(exc):
                continue
            raise
        labels = inspected.get('Labels') if kind == 'volume' else inspected.get('Config', {}).get('Labels')
        if not isinstance(labels, dict) or labels.get(LABEL) != nonce:
            raise SandboxError('resource nonce label mismatch')
        if kind == 'container':
            identifier = inspected.get('Id')
            if not isinstance(identifier, str) or not re.fullmatch(r'[0-9a-f]{64}', identifier):
                raise SandboxError('invalid container identity')
            present.append((kind, identifier))
        else:
            present.append((kind, resource))
    if root.exists() or root.is_symlink():
        if root.is_symlink() or not root.is_dir() or root.stat().st_uid != os.getuid():
            raise SandboxError('staging ownership mismatch')
        marker = root / '.owner'
        if marker.is_symlink() or marker.read_text() != nonce:
            raise SandboxError('staging nonce mismatch')
    # Verify all identities before mutation. Names cannot be claimed from prefixes.
    for kind, resource in present:
        docker(['rm', '-f', resource] if kind == 'container' else ['volume', 'rm', resource])
    if root.exists():
        shutil.rmtree(root)


class DockerSandbox:
    def __init__(self, config, skill_text, files, *, trial_id='', resource_recorder=None):
        for field in ('max_file_bytes', 'max_total_bytes', 'max_files'):
            if type(config[field]) is not int or config[field] <= 0:
                raise ValueError('invalid sandbox storage bound: ' + field)
        timeout = config['timeout_seconds']
        if isinstance(timeout, bool) or not isinstance(timeout, (int, float)) or not math.isfinite(timeout) or timeout <= 0:
            raise ValueError('invalid sandbox timeout')
        self.config = config
        self.skill_text = skill_text
        self.files = files
        self.nonce = uuid.uuid4().hex
        self.name = 'skill-evolver-' + self.nonce
        self.staging = Path(tempfile.gettempdir()) / self.name
        self.stopped = False
        self.created = False
        self.allocated = False
        self.environment_hash = ''
        self.image_id = ''
        self.deadline = None
        self.resource_recorder = resource_recorder
        self.event = {'kind': 'resource_allocated', 'resource_id': self.nonce, 'trial_id': trial_id,
                      'resources': {'main': self.name, 'keeper': self.name + '-keeper', 'volume': self.name,
                                    'labels': {LABEL: self.nonce},
                                    'staging': {'path': str(self.staging), 'nonce': self.nonce, 'uid': os.getuid()}}}
        # Linux tmpfs allocates pages. Exact logical-byte limits remain in collector.
        self.storage_bytes = ((config['max_total_bytes'] + 4095) // 4096) * 4096
        self.storage_inodes = config['max_files'] + 1

    def _docker(self, args, *, timeout=15, limit=1024 * 1024, input_bytes=None):
        return _bounded_process(['docker', *args], timeout=timeout, limit=limit, input_bytes=input_bytes)

    def start(self):
        for path, content in self.files.items():
            relative_path(path)
            if not isinstance(content, str):
                raise ValueError('input content must be text')
        self.image_id = resolve_image(self.config['image'])
        if self.resource_recorder:
            self.resource_recorder(copy_event(self.event))
        self.allocated = True
        self.staging.mkdir(mode=0o700)
        (self.staging / '.owner').write_text(self.nonce)
        root = self.staging
        inputs = root / 'input'
        inputs.mkdir(mode=0o755)
        for name, content in self.files.items():
            target = inputs / name
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(content, encoding='utf-8')
            target.chmod(0o444)
        (root / 'SKILL.md').write_text(self.skill_text, encoding='utf-8')
        (root / 'SKILL.md').chmod(0o444)
        root.chmod(0o755)
        options = f'size={self.storage_bytes},nr_inodes={self.storage_inodes},uid=65534,gid=65534,mode=0770'
        self._docker(['volume', 'create', '--driver', 'local', '--label', f'{LABEL}={self.nonce}',
                      '--opt', 'type=tmpfs', '--opt', 'device=tmpfs', '--opt', 'o=' + options, self.name])
        inspected = json.loads(self._docker(['volume', 'inspect', self.name]))[0]
        if inspected.get('Driver') != 'local' or inspected.get('Options') != {'type': 'tmpfs', 'device': 'tmpfs', 'o': options}:
            raise SandboxError('bounded output storage not established')
        common = ['--pull', 'never', '--network', 'none', '--cap-drop', 'ALL',
                  '--security-opt', 'no-new-privileges', '--read-only', '--pids-limit', '64',
                  '--memory', '128m', '--memory-swap', '128m', '--cpus', '1', '--user', '0:0',
                  '--label', f'{LABEL}={self.nonce}']
        mount = ['--mount', f'type=volume,src={self.name},dst=/output,volume-nocopy']
        # Trusted PID1s receive only bounded sleep durations, never model commands.
        lifetime = float(self.config['timeout_seconds'])
        self._docker(['run', '-d', '--name', self.name + '-keeper', *common, *mount,
                      '--entrypoint', '/bin/sleep', self.image_id, str(lifetime + COLLECTION_GRACE_SECONDS)])
        if self._docker(['inspect', '--format', '{{.State.Running}}', self.name + '-keeper']).strip() != b'true':
            raise SandboxError('output keeper not running')
        args = ['create', '--name', self.name, *common, '--workdir', '/output',
                '--mount', f'type=bind,src={inputs},dst=/input,readonly',
                '--mount', f'type=bind,src={root / "SKILL.md"},dst=/SKILL.md,readonly', *mount,
                '--tmpfs', '/tmp:rw,noexec,nosuid,size=16m,nr_inodes=128',
                '--entrypoint', '/bin/sleep', self.image_id, str(lifetime)]
        self._docker(args)
        self.created = True
        main_started = time.monotonic()
        self._docker(['start', self.name])
        if self._docker(['exec', '--user', '65534:65534', self.name, '/bin/stat', '-f', '-c', '%T:%S', '/output']).strip() != b'tmpfs:4096':
            raise SandboxError('output is not bounded tmpfs with supported 4096-byte pages')
        self.deadline = main_started + lifetime
        contract = {'image': self.image_id, 'harness': 2, 'storage_bytes': self.storage_bytes,
                    'storage_inodes': self.storage_inodes, 'timeout_seconds': lifetime,
                    'logical_limits': {k: self.config[k] for k in ('max_file_bytes', 'max_total_bytes', 'max_files')},
                    'supervisor': 'root-sleep', 'candidate_user': '65534:65534', 'flags': common}
        contract['flags'] = common[:-2]  # nonce label is ownership, not environment identity
        self.environment_hash = hashlib.sha256(json.dumps(contract, sort_keys=True).encode()).hexdigest()
        return self

    def tool(self, name, arguments):
        if self.stopped or self.deadline is None:
            raise SandboxError('container is not running')
        timeout = self.deadline - time.monotonic()
        if timeout <= 0:
            raise TimeoutError('trial deadline exceeded')
        if name == 'run_command':
            command = arguments['command']
            if not isinstance(command, str):
                raise ValueError('command must be text')
            return self._docker(['exec', '--user', '65534:65534', self.name, '/bin/sh', '-c', command], timeout=timeout,
                                limit=self.config['max_total_bytes']).decode('utf-8', 'replace')
        path = arguments.get('path', '/output')
        if not isinstance(path, str) or not (path == '/SKILL.md' or path in ('/input', '/output') or path.startswith(('/input/', '/output/'))):
            raise ValueError('tool path outside input/output')
        if '..' in path.split('/') or '\x00' in path:
            raise ValueError('unsafe tool path')
        if name == 'read_file':
            args = ['cat', '--', path]
            data = None
        elif name == 'list_files':
            args = ['find', path, '-type', 'f']
            data = None
        elif name == 'write_file':
            if not path.startswith('/output/'):
                raise ValueError('writes require /output')
            data = arguments['content'].encode('utf-8')
            if len(data) > self.config['max_file_bytes']:
                raise ValueError('write exceeds file limit')
            args = ['/bin/sh', '-c', 'cat > "$1"', 'write_file', path]
        else:
            raise ValueError('unsupported tool')
        return self._docker(['exec', '--user', '65534:65534', '-i', self.name, *args], timeout=timeout,
                            limit=self.config['max_total_bytes'], input_bytes=data).decode('utf-8', 'replace')

    def stop(self):
        if not self.created:
            return
        state = self._docker(['inspect', '--format', '{{.State.Running}}', self.name]).strip()
        if state == b'true':
            try:
                self._docker(['kill', self.name])
            except SandboxError:
                # It may naturally expire between inspect and kill.
                if self._docker(['inspect', '--format', '{{.State.Running}}', self.name]).strip() != b'false':
                    raise
        if self._docker(['inspect', '--format', '{{.State.Running}}', self.name]).strip() != b'false':
            raise SandboxError('container stop not proven')
        self.stopped = True

    def collect(self, output_paths):
        if not self.stopped:
            raise SandboxError('collection requires proven stop')
        if self._docker(['inspect', '--format', '{{.State.Running}}', self.name + '-keeper']).strip() != b'true':
            raise SandboxError('output keeper expired; artifacts unavailable')
        # Tar framing is bounded too; malicious files cannot exhaust host memory.
        cap = self.config['max_total_bytes'] + 4096 * (self.config['max_files'] + 32)
        try:
            data = self._docker(['cp', self.name + ':/output/.', '-'], limit=cap)
        except SandboxError as exc:
            if 'output limit exceeded' in str(exc):
                raise ArtifactError('artifact archive limit exceeded') from exc
            raise
        return collect_tar(io.BytesIO(data), output_paths, **{k: self.config[k] for k in ('max_file_bytes', 'max_total_bytes', 'max_files')})

    def close(self):
        if not self.allocated:
            return
        recover_allocation(self.event)
        self.allocated = False
        if self.resource_recorder:
            released = copy_event(self.event)
            released['kind'] = 'resource_released'
            self.resource_recorder(released)


def copy_event(event):
    return json.loads(json.dumps(event))
