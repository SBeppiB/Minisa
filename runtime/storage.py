"""Cooperating awake writers share one root lock; files publish atomically.

This advisory boundary is not protection against old code or operator writes.
Never run an older writer against the same root concurrently.
"""
from contextlib import contextmanager
from functools import wraps
import fcntl
import os
from pathlib import Path
import re
import tempfile
import threading

_registry = {}
_registry_lock = threading.Lock()


@contextmanager
def writer_lock(root, *, blocking=True):
    root = Path(root).resolve()
    key = (os.getpid(), str(root))
    with _registry_lock:
        lock, local = _registry.setdefault(key, (threading.RLock(), threading.local()))
    if not lock.acquire(blocking=blocking):
        raise BlockingIOError("Awake cognition owns the writer lock")
    try:
        if getattr(local, "depth", 0):
            local.depth += 1
            try:
                yield
            finally:
                local.depth -= 1
            return
        root.mkdir(parents=True, exist_ok=True)
        with write_authority(root):
            fd = os.open(root / ".minisa-writer.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | (0 if blocking else fcntl.LOCK_NB))
                local.depth = 1
                try:
                    yield
                finally:
                    local.depth = 0
            finally:
                os.close(fd)
    finally:
        lock.release()


def serialized(method):
    @wraps(method)
    def call(self, *args, **kwargs):
        with writer_lock(self.root):
            return method(self, *args, **kwargs)
    return call


def identifier(value):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        raise ValueError("Invalid record identifier")
    return value


def sync_directories(*paths):
    warnings = []
    for path in dict.fromkeys(paths):
        try:
            fd = os.open(path, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        except OSError as error:
            warnings.append(f"Directory durability not confirmed: {error}")
    return warnings


def check_target(root, path):
    root = Path(root).absolute()
    path = Path(path).absolute()
    if ".." in path.parts or not path.is_relative_to(root):
        raise ValueError("Write target escapes the configured root")
    for ancestor in (path, *path.parents):
        if ancestor == root:
            break
        if ancestor.is_symlink():
            raise ValueError("Symlink write target is outside writer authority")


def atomic_write(path, content, *, root, replace=False):
    """Return explicit outcome after publication; raise only before publication."""
    path = Path(path)
    check_target(root, path)
    path.parent.mkdir(parents=True, exist_ok=True)
    if path.is_symlink():
        raise ValueError("Refusing a symlink write target")
    temporary = None
    committed = False
    warnings = []
    try:
        with tempfile.NamedTemporaryFile(mode="w", encoding="utf-8", dir=path.parent,
                                         prefix=".minisa-", delete=False) as stream:
            temporary = Path(stream.name)
            if replace and path.exists():
                os.fchmod(stream.fileno(), path.stat().st_mode & 0o777)
            stream.write(content)
            stream.flush()
            os.fsync(stream.fileno())
        if replace:
            os.replace(temporary, path)
        else:
            # An atomic no-clobber publication of already complete bytes.
            os.link(temporary, path)
        committed = True
        directories = []
        parent = path.absolute().parent
        while True:
            directories.append(parent)
            if parent == Path(root).absolute():
                break
            parent = parent.parent
        warnings.extend(sync_directories(*directories))
    finally:
        if temporary is not None and temporary.exists():
            try:
                temporary.unlink()
            except OSError as error:
                if not committed:
                    raise
                warnings.append(f"Staging cleanup failed: {error}")
    return {"status": "committed", "committed": True, "durable": not warnings,
            "path": str(path), "warnings": warnings}


_authorities = {}


class WriterAuthority:
    """Exclusive awake service lease; OS releases it on process termination."""
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.fd = None

    def acquire(self):
        key = (os.getpid(), str(self.root))
        with _registry_lock:
            if key in _authorities:
                raise RuntimeError("An awake writer already owns this resource root")
            self.root.mkdir(parents=True, exist_ok=True)
            fd = os.open(self.root / ".minisa-awake.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
            try:
                fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
            except OSError:
                os.close(fd)
                raise RuntimeError("An awake writer already owns this resource root") from None
            self.fd = fd
            _authorities[key] = fd

    def release(self):
        with _registry_lock:
            if self.fd is not None:
                _authorities.pop((os.getpid(), str(self.root)), None)
                os.close(self.fd)
                self.fd = None


@contextmanager
def write_authority(root):
    key = (os.getpid(), str(root))
    with _registry_lock:
        owned = key in _authorities
    if owned:
        yield
        return
    fd = os.open(root / ".minisa-awake.lock", os.O_CREAT | os.O_RDWR | os.O_NOFOLLOW, 0o600)
    try:
        try:
            fcntl.flock(fd, fcntl.LOCK_SH | fcntl.LOCK_NB)
        except OSError:
            raise PermissionError("Persistent writes belong to the running awake service; use its API") from None
        yield
    finally:
        os.close(fd)
