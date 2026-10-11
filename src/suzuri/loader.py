from __future__ import annotations
from os import PathLike
from pathlib import Path
from threading import RLock
from asyncio import to_thread
from functools import partial
from dataclasses import replace
from typing import Any, Literal
from suzuri.cache import _TemplateCache
from suzuri.html import template_comment
from suzuri.errors import TemplateNotFound
from suzuri.asynchronous import _async_stream
from suzuri.artifacts import _ArtifactStore, _RendererLoader
from importlib.util import module_from_spec, spec_from_loader
from collections.abc import Mapping, Callable, Generator, AsyncGenerator
from suzuri._validation import validate_template_name, validate_compile_options
from suzuri.rendering import Template, RenderEvent, _RenderOptions, _CompiledTemplate

class Engine:
    """Compile source views on their first render and reuse their persistent cache.

    directory is the template source directory. compiled_dir defaults to a sibling
    named compiled_views. Source edits are ignored until the cached view is deleted.
    Minification and metadata comments are baked into the generated HTML literals;
    script/style contents are preserved and streams remain lazy.

    An existing build() bundle can still be opened by omitting compiled_dir.
    Templates, cache files and helper modules must be trusted application code.
    """

    def __init__(
        self, directory: str | PathLike[str], *, compiled_dir: str | PathLike[str] | None = None,
        application_version: str | None = None,
        preload: bool = False, default_context: Mapping[str, Any] | None = None,
        chunk_size: int = 8192, verify_artifacts: Literal['eager', 'on_load'] = 'eager',
        on_render: Callable[[RenderEvent], object] | None = None,
        optimize: bool = True, minify_html: bool = False, template_comments: bool = False,
    ) -> None:
        if type(preload) is not bool:
            raise TypeError('preload must be a boolean')

        if not isinstance(verify_artifacts, str):
            raise TypeError("verify_artifacts must be 'eager' or 'on_load'")

        if verify_artifacts not in ('eager', 'on_load'):
            raise ValueError("verify_artifacts must be 'eager' or 'on_load'")

        if application_version is not None and not isinstance(application_version, str):
            raise TypeError('application_version must be a string or None')

        validate_compile_options(optimize=optimize, minify_html=minify_html, template_comments=template_comments)
        self._options = _RenderOptions(
            default_context={} if default_context is None else default_context,
            chunk_size=chunk_size, on_render=on_render,
            minify_html=minify_html, template_comments=template_comments,
        )
        self._path = Path(directory).resolve()
        self._preload = preload
        self._verify_artifacts = verify_artifacts
        self._optimize = optimize
        self._lock = RLock()
        self._loaded: dict[str, _CompiledTemplate] = {}
        self._templates: dict[str, Template] = {}
        self._stores: dict[str, _ArtifactStore] = {}
        self._artifacts: _ArtifactStore | None = None
        self._cache: _TemplateCache | None = None
        if compiled_dir is None and (self.path / 'manifest.json').is_file():
            self._compiled_dir = self.path
            self._artifacts = _ArtifactStore(self.path, application_version, eager=verify_artifacts == 'eager')
        else:
            self._compiled_dir = (
                self.path.parent / 'compiled_views' if compiled_dir is None else Path(compiled_dir).resolve()
            )
            self._cache = _TemplateCache(
                self.path, self.compiled_dir, application_version=application_version or '',
                optimize=optimize, minify_html=minify_html, template_comments=template_comments,
            )

        if preload:
            for name in self.template_names:
                self._get_compiled(name)

    @property
    def path(self) -> Path:
        return self._path

    @property
    def compiled_dir(self) -> Path:
        return self._compiled_dir

    @property
    def optimize(self) -> bool:
        """Whether to optimize newly compiled templates; existing bundles are unchanged."""
        return self._optimize

    @property
    def preload(self) -> bool:
        return self._preload

    @property
    def default_context(self) -> Mapping[str, Any]:
        return self._options.default_context

    @property
    def chunk_size(self) -> int:
        return self._options.chunk_size

    @property
    def verify_artifacts(self) -> Literal['eager', 'on_load']:
        return self._verify_artifacts

    @property
    def on_render(self) -> Callable[[RenderEvent], object] | None:
        return self._options.on_render

    @property
    def minify_html(self) -> bool:
        return self._options.minify_html

    @property
    def template_comments(self) -> bool:
        return self._options.template_comments

    @property
    def template_names(self) -> tuple[str, ...]:
        """Sorted names in the source directory or bundle, without compiling templates."""
        if self._artifacts is not None:
            return tuple(sorted(self._artifacts.modules))

        return tuple(sorted(path.relative_to(self.path).as_posix() for path in self.path.rglob('*.html') if path.is_file()))

    def get_template(self, name: str) -> Template:
        """Get a reusable handle by relative POSIX name without compiling or importing helpers.

        Raise TemplateNotFound when neither a source nor a cached template exists.
        Existing bundles use their manifest to determine which templates exist.
        """
        if isinstance(name, str):
            cached = self._templates.get(name)
            if cached is not None:
                return cached

        validate_template_name(name)
        with self._lock:
            if name not in self._templates:
                if self._cache is not None:
                    self._cache.validate_name(name)
                elif self._artifacts is None or name not in self._artifacts.modules:
                    raise TemplateNotFound(name, self.path)

                self._templates[name] = Template(name, partial(self._get_compiled, name), self._options)

            return self._templates[name]

    def render(self, name: str, context: Mapping[str, Any] | None = None) -> str:
        """Render a named template with per-call values overriding default_context."""
        return self.get_template(name).render(context)

    def iter_render(
        self, name: str, context: Mapping[str, Any] | None = None, *, chunk_size: int | None = None,
    ) -> Generator[str]:
        """Snapshot context now and stream lazily; close() releases a partial stream."""
        return self.get_template(name).iter_render(context, chunk_size=chunk_size)

    async def render_async(self, name: str, context: Mapping[str, Any] | None = None) -> str:
        """Render in a worker thread, taking a context snapshot when awaited."""
        validate_template_name(name)
        values = self._options.context(context)
        return await to_thread(self._render, name, values)

    def iter_render_async(
        self, name: str, context: Mapping[str, Any] | None = None, *, chunk_size: int | None = None,
    ) -> AsyncGenerator[str]:
        """Snapshot context now and stream in worker threads; close with aclose()."""
        validate_template_name(name)
        size = self._options.size(chunk_size)
        values = self._options.context(context)
        return _async_stream(partial(self._iter_render, name, values, size))

    def _render(self, name: str, values: Mapping[str, Any]) -> str:
        return self.get_template(name)._render(values)

    def _iter_render(self, name: str, values: Mapping[str, Any], size: int) -> Generator[str]:
        yield from self.get_template(name)._iter_render(values, size)

    def _get_compiled(self, name: str) -> _CompiledTemplate:
        if self._cache is None and name in self._loaded:
            return self._loaded[name]

        with self._lock:
            artifacts = self._cache.get(name) if self._cache is not None else self._artifacts
            if artifacts is None or name not in artifacts.modules:
                raise TemplateNotFound(name, self.path)

            if name in self._loaded and self._stores[name] is artifacts:
                return self._loaded[name]

            module_name = artifacts.modules[name]
            filename = module_name + '.py'
            code = artifacts.load_code(name)
            path = str(artifacts.path / filename)
            loader = _RendererLoader(path, code)
            spec = spec_from_loader('_suzuri_bundle_' + module_name, loader, origin=path)
            assert spec is not None
            module = module_from_spec(spec)
            module.__file__ = path
            loader.exec_module(module)
            compiled_comment = getattr(module, '__suzuri_template_comment__', '')
            comment = template_comment(artifacts.template_metadata(name)) if self.template_comments and not compiled_comment else ''
            options = self._options
            if self.minify_html and getattr(module, '__suzuri_minified__', False):
                options = replace(options, minify_html=False)

            template = _CompiledTemplate(name, module, options, comment, compiled_comment)
            self._loaded[name] = template
            self._stores[name] = artifacts
            return template
