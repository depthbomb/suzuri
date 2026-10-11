from __future__ import annotations
from json import loads
from typing import Any
from pathlib import Path
from hashlib import sha256
from types import CodeType
from importlib.abc import InspectLoader
from suzuri.errors import ArtifactError
from suzuri.runtime import require_abi, require_python, ESCAPING_POLICY

class _RendererLoader(InspectLoader):
    """Expose the verified source to execution and traceback inspection."""

    def __init__(self, filename: str, source: bytes) -> None:
        self.filename = filename
        self.source = source

    def get_source(self, fullname: str) -> str:
        return self.source.decode('utf-8')

    def get_code(self, fullname: str) -> CodeType:
        return compile(self.source, self.filename, 'exec', dont_inherit=True)

class _ArtifactStore:
    """Keep verified renderer bytes so loading never executes a second disk read."""

    def __init__(self, path: Path, application_version: str | None, *, eager: bool) -> None:
        self.path = path
        self.modules: dict[str, str] = {}
        self._hashes: dict[str, str] = {}
        self._members: dict[str, tuple[str, ...]] = {}
        self._code: dict[str, bytes] = {}
        self._verified: set[str] = set()
        try:
            manifest = loads((path / 'manifest.json').read_text(encoding='utf-8'))
            if not isinstance(manifest, dict) or type(manifest.get('schema')) is not int or manifest['schema'] != 1:
                raise ArtifactError('Unsupported bundle manifest schema')

            self._read_manifest(manifest, application_version)
            self._manifest = manifest
            assigned = {name for members in self._members.values() for name in members}
            self._shared = tuple(name for name in self._hashes if name not in assigned)
            self._renderers = {module + '.py' for module in self.modules.values()}
            if eager:
                for name in self._hashes:
                    self._verify(name)
        except (OSError, ValueError, KeyError, TypeError) as error:
            raise ArtifactError(f'Cannot load bundle {path}: {error}') from error

    def _read_manifest(self, manifest: dict[str, Any], application_version: str | None) -> None:
        require_abi(manifest['runtime_abi'], manifest.get('compiler_revision', 1))
        require_python(manifest.get('python_requires', ''))

        if manifest['escaping_policy'] != ESCAPING_POLICY:
            raise ArtifactError('Incompatible escaping policy; rebuild the bundle')

        if application_version is not None and manifest.get('application_version') != application_version:
            raise ArtifactError('Application dependency version does not match the bundle')

        artifacts = manifest['artifacts']
        templates = manifest['templates']
        if not isinstance(artifacts, dict) or not isinstance(templates, dict):
            raise ArtifactError('Malformed artifact or template table')

        for name, digest in artifacts.items():
            if (
                not isinstance(name, str) or not name or name in ('.', '..')
                or Path(name).name != name or any(char in name for char in '/\\:\0')
            ):
                raise ArtifactError('Artifact paths must be plain filenames')

            if not isinstance(digest, str) or len(digest) != 64 or any(c not in '0123456789abcdef' for c in digest):
                raise ArtifactError(f'Invalid artifact hash: {name}')

            self._hashes[name] = digest

        for name, entry in templates.items():
            if not isinstance(entry, dict):
                raise ArtifactError('Malformed template entry')

            module = entry['module']
            if not isinstance(name, str) or not isinstance(module, str) or not module.isidentifier():
                raise ArtifactError('Invalid template or module name')

            if module + '.py' not in self._hashes:
                raise ArtifactError(f'Missing compiled template: {name}')

            self.modules[name] = module
            self._members[name] = tuple(
                module + suffix for suffix in ('.py', '.pyi', '_check.py', '.map.json')
                if module + suffix in self._hashes
            )

    def _verify(self, name: str) -> None:
        if name in self._verified:
            return

        path = (self.path / name).resolve()
        if not path.is_relative_to(self.path):
            raise ArtifactError('Artifact path escapes the bundle')

        content = path.read_bytes()
        if sha256(content).hexdigest() != self._hashes[name]:
            raise ArtifactError(f'Artifact hash mismatch: {name}')

        if name in self._renderers:
            self._code[name] = content

        self._verified.add(name)

    def load_code(self, name: str) -> bytes:
        try:
            for artifact in (*self._shared, *self._members[name]):
                self._verify(artifact)
        except OSError as error:
            raise ArtifactError(f'Cannot load template {name!r} from {self.path}: {error}') from error

        return self._code[self.modules[name] + '.py']

    def template_metadata(self, name: str) -> dict[str, object]:
        sources = self._manifest.get('sources', {})
        dependencies = self._manifest['templates'][name].get('dependencies', [])
        compiler_version = self._manifest.get('compiler_version', '')
        application_version = self._manifest.get('application_version', '')
        if (
            not isinstance(sources, dict)
            or not isinstance(dependencies, list)
            or any(not isinstance(item, str) for item in dependencies)
            or not isinstance(compiler_version, str)
            or not isinstance(application_version, str)
        ):
            raise ArtifactError(f'Malformed template metadata: {name}')

        source_hash = sources.get(name, '')
        if not isinstance(source_hash, str):
            raise ArtifactError(f'Malformed template source hash: {name}')

        return {
            'name': name,
            'source_sha256': source_hash,
            'includes': [item for item in dependencies if item != name],
            'compiler_version': compiler_version,
            'application_version': application_version,
        }
