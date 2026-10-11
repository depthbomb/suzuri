from __future__ import annotations
import sys
from json import dumps
from time import sleep
from uuid import uuid4
from glob import escape
from pathlib import Path
from shutil import rmtree
from hashlib import sha256
from suzuri.build import build
from dataclasses import dataclass
from collections.abc import Generator
from contextlib import contextmanager
from suzuri._validation import validate_template_name
from suzuri.artifacts import _ArtifactStore, _bytecode_format
from suzuri.errors import BuildError, ArtifactError, TemplateNotFound
from suzuri.runtime import VERSION, ABI_VERSION, PYTHON_REQUIRES, ESCAPING_POLICY, COMPILER_REVISION

if sys.platform == 'win32':
    from msvcrt import locking, LK_NBLCK, LK_UNLCK
else:
    from fcntl import flock, LOCK_EX, LOCK_UN

@dataclass
class _CacheEntry:
    store: _ArtifactStore
    files: tuple[Path, ...]
    signature: tuple[tuple[int, int, int], ...]

def _signature(files: tuple[Path, ...]) -> tuple[tuple[int, int, int], ...]:
    stats = (path.stat() for path in files)
    return tuple((stat.st_ino, stat.st_mtime_ns, stat.st_size) for stat in stats)

@contextmanager
def _file_lock(path: Path) -> Generator[None]:
    # Keep lock files in place: removing one can give two workers different locks.
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open('a+b') as handle:
        if sys.platform == 'win32':
            if handle.seek(0, 2) == 0:
                handle.write(b'\0')
                handle.flush()

            handle.seek(0)
            while True:
                try:
                    locking(handle.fileno(), LK_NBLCK, 1)
                    break
                except OSError as error:
                    if error.errno not in (13, 36):
                        raise

                    sleep(0.01)
        else:
            flock(handle.fileno(), LOCK_EX)

        try:
            yield
        finally:
            if sys.platform == 'win32':
                handle.seek(0)
                locking(handle.fileno(), LK_UNLCK, 1)
            else:
                flock(handle.fileno(), LOCK_UN)

class _TemplateCache:
    """Persist each requested view separately, without checking source freshness."""

    def __init__(
        self, source: Path, directory: Path, *, application_version: str,
        optimize: bool, minify_html: bool, template_comments: bool, bytecode: bool,
    ) -> None:
        if not source.is_dir():
            raise BuildError(f'Template directory does not exist: {source}')

        if directory.is_relative_to(source) or source.is_relative_to(directory):
            raise BuildError('Source and compiled directories must not overlap')

        self.source = source
        self.directory = directory
        self.application_version = application_version
        self.optimize = optimize
        self.minify_html = minify_html
        self.template_comments = template_comments
        self.bytecode = bytecode
        self._entries: dict[str, _CacheEntry] = {}
        identity: dict[str, object] = {
            'source': str(source), 'compiler': VERSION, 'abi': ABI_VERSION,
            'compiler_revision': COMPILER_REVISION,
            'python_requires': PYTHON_REQUIRES,
            'escaping': ESCAPING_POLICY, 'application': application_version,
            'minify_html': minify_html, 'template_comments': template_comments,
        }
        if not optimize:
            # Separate reference output from optimized views within this revision.
            identity['optimize'] = False

        if bytecode:
            identity['bytecode'] = _bytecode_format()

        self._identity = dumps(identity, sort_keys=True)

    def validate_name(self, name: str) -> None:
        validate_template_name(name)
        requested = self.source / name
        if not requested.resolve().is_relative_to(self.source):
            raise ValueError('Template path escapes the source directory')

        if not requested.is_file() and not (self._path(name) / 'manifest.json').is_file():
            raise TemplateNotFound(name, self.source)

    def get(self, name: str) -> _ArtifactStore:
        cached = self._entries.get(name)
        if cached is not None:
            try:
                if _signature(cached.files) == cached.signature:
                    return cached.store
            except OSError:
                pass

        path = self._path(name)
        # Locks live beside the cache so deleting the cache cannot split a live lock.
        lock_path = self.directory.with_name(self.directory.name + '.locks') / (path.name + '.lock')
        with _file_lock(lock_path):
            try:
                store = self._read(path, name)
            except ArtifactError:
                self.validate_name(name)
                if not (self.source / name).is_file():
                    raise TemplateNotFound(name, self.source) from None

                self._compile(path, name)
                store = self._read(path, name)

            files = (path / 'manifest.json', *store.renderer_files(name))
            self._entries[name] = _CacheEntry(store, files, _signature(files))
            return store

    def _path(self, name: str) -> Path:
        key = 't_' + sha256((self._identity + '\0' + name).encode('utf-8')).hexdigest()[:32]
        return self.directory / key

    def _read(self, path: Path, name: str) -> _ArtifactStore:
        store = _ArtifactStore(path, self.application_version, eager=True)
        if name not in store.modules:
            raise ArtifactError(f'Missing compiled template: {name}')

        store.load_code(name)
        return store

    def _compile(self, path: Path, name: str) -> None:
        staging = path.with_name('.' + path.name + '-' + uuid4().hex)
        retired = path.with_name('.' + path.name + '-old-' + uuid4().hex)
        try:
            build(
                self.source, staging, pattern=escape(name),
                application_version=self.application_version, optimize=self.optimize,
                minify_html=self.minify_html, template_comments=self.template_comments,
                bytecode=self.bytecode,
            )
            if path.exists():
                path.rename(retired)

            try:
                staging.rename(path)
            except OSError:
                if retired.exists():
                    retired.rename(path)

                raise
        finally:
            for temporary in (staging, retired):
                if temporary.is_dir():
                    rmtree(temporary)
