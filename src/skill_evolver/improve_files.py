"""Read-only snapshot of a skill's companion files; scripts are passed as text, never executed."""
import errno
import hashlib
import os
from pathlib import Path
import posixpath
import re
import stat
from .genome import local_targets, split_skill

COMPANION_DIRS = ('references', 'assets', 'scripts')
# Generated or environment files that are never part of a skill's content.
IGNORED_PARTS = {'__pycache__', 'node_modules'}
IGNORED_SUFFIXES = ('.pyc', '.pyo')
# Files the model is expected to read; they must decode, never become a placeholder.
TEXT_SUFFIXES = ('.md', '.txt', '.py', '.sh', '.js', '.ts', '.json', '.yaml', '.yml', '.toml', '.csv', '.html')
MAX_FILES = 32
FILE_BYTES = 64 * 1024
BINARY_BYTES = 16 * 1024 * 1024
TOTAL_BYTES = 128 * 1024
# A path such as `references/guide.md`, but not `app/assets/x` inside a longer path.
MENTION = re.compile(r'(?<![\w./-])((?:scripts|references|assets)/[^\s`\'")>\]]+)')
GLOB = re.compile(r'[*?\[{]')


def _normal(path):
    path = posixpath.normpath(path.split('#')[0])
    return '' if path == '.' else path


def mentioned(text):
    """Companion-looking paths written in the body (may name files the skill creates)."""
    body = split_skill(text)[1]
    paths = {_normal(match.rstrip('.,;:*_~')) for match in MENTION.findall(body)}
    return {path for path in paths if path and not GLOB.search(path)}


def linked(text):
    return {_normal(path) for path in local_targets(split_skill(text)[1])} - {''}


def _ignored(relative):
    parts = relative.split('/')
    return (any(part.startswith('.') or part in IGNORED_PARTS for part in parts)
            or relative.endswith(IGNORED_SUFFIXES))


def _inside(root, relative):
    parts = relative.split('/')
    if Path(relative).is_absolute() or '..' in parts:
        raise ValueError('unsupported: companion path outside the skill: ' + relative)
    if any(part.startswith('.') for part in parts):
        raise ValueError('unsupported: companion files must not be hidden: ' + relative)
    for index in range(1, len(parts)):
        if root.joinpath(*parts[:index]).is_symlink():
            raise ValueError('unsupported: companion files must not be symlinks: ' + relative)
    return root.joinpath(*parts)


def _read(path, relative):
    """Open without following links and re-check type and size on the opened descriptor."""
    try:
        # O_NONBLOCK keeps a FIFO from blocking the open; it is rejected as non-regular below.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    except FileNotFoundError:
        return None
    except OSError as exc:
        reason = 'must not be symlinks' if exc.errno == errno.ELOOP else 'must be readable'
        raise ValueError('unsupported: companion files {}: {}'.format(reason, relative)) from exc
    info = os.fstat(descriptor)
    if stat.S_ISDIR(info.st_mode):
        os.close(descriptor)
        return False
    with os.fdopen(descriptor, 'rb') as handle:
        if not stat.S_ISREG(info.st_mode) or info.st_nlink > 1:
            raise ValueError('unsupported: companion files must be regular, unlinked files: ' + relative)
        data = handle.read(FILE_BYTES + 1)
        if _binary(data) and relative.endswith(TEXT_SUFFIXES):
            raise ValueError('unsupported: companion text files must be UTF-8: ' + relative)
        if not _binary(data):
            if len(data) > FILE_BYTES:
                raise ValueError('unsupported: companion file exceeds 64 KiB: ' + relative)
            return data.decode('utf-8', 'strict')
        # Binary content is never sent; its size and hash still detect changes.
        digest, size = hashlib.sha256(data), len(data)
        while chunk := handle.read(1024 * 1024):
            digest.update(chunk)
            size += len(chunk)
            if size > BINARY_BYTES:
                raise ValueError('unsupported: companion file exceeds 16 MiB: ' + relative)
        return '[binary file not sent: {} bytes, sha256 {}]'.format(size, digest.hexdigest())


def _binary(data):
    try:
        data.decode('utf-8')
    except UnicodeDecodeError as exc:
        # A multi-byte character cut at the read boundary is still text.
        return not (len(data) > FILE_BYTES and exc.start >= len(data) - 3 and exc.reason == 'unexpected end of data')
    return False


def snapshot(skill_dir, text):
    """Return ({relative path: text}, [mentioned paths that do not exist])."""
    root = Path(skill_dir)
    links = linked(text)
    paths = mentioned(text) | links
    for name in COMPANION_DIRS:
        base = root / name
        if base.is_symlink():
            raise ValueError('unsupported: companion files must not be symlinks: ' + name)
        if base.is_dir():
            for path in base.rglob('*'):
                relative = path.relative_to(root).as_posix()
                if not _ignored(relative) and (path.is_symlink() or not path.is_dir()):
                    paths.add(relative)
    files, missing, total = {}, [], 0
    for relative in sorted(paths):
        content = _read(_inside(root, relative), relative)
        if content is None:
            if relative in links:
                raise ValueError('unsupported: linked companion file is missing: ' + relative)
            missing.append(relative)
        elif content is not False:
            files[relative] = content
            total += len(content.encode())
    if len(files) > MAX_FILES:
        raise ValueError('unsupported: companion files exceed {} files'.format(MAX_FILES))
    if total > TOTAL_BYTES:
        raise ValueError('unsupported: companion files exceed 128 KiB in total')
    return files, missing


def check_references(text, files, original):
    """A revised skill may refer only to snapshot files or paths the original already mentioned."""
    allowed = set(files) | mentioned(original)
    extra = sorted((mentioned(text) | linked(text)) - allowed)
    if extra:
        raise ValueError('candidate references files outside the snapshot: ' + ', '.join(extra))


def validate_files(value):
    if (not isinstance(value, dict) or len(value) > MAX_FILES
            or not all(isinstance(k, str) and isinstance(v, str) for k, v in value.items())):
        raise ValueError('invalid companion snapshot')
    return value
