from __future__ import annotations
from asyncio import to_thread
from functools import partial
from logging import getLogger
from time import perf_counter
from typing import Any, Literal
from suzuri.html import minify_html
from suzuri.runtime import ensure_sync
from dataclasses import field, dataclass
from suzuri.asynchronous import _async_stream
from types import ModuleType, MappingProxyType
from suzuri._validation import validate_chunk_size
from collections.abc import Mapping, Callable, Generator, AsyncGenerator
from inspect import isasyncgen, isgenerator, isasyncgenfunction, iscoroutinefunction, isgeneratorfunction

@dataclass(frozen=True)
class RenderEvent:
    """The outcome of a buffered render or a started stream, without its context."""

    template_name: str
    duration_seconds: float
    status: Literal['completed', 'closed', 'error']
    streaming: bool
    output_chars: int
    error: BaseException | None = None

@dataclass(frozen=True)
class _RenderOptions:
    default_context: Mapping[str, Any] = field(default_factory=dict)
    chunk_size: int = 8192
    on_render: Callable[[RenderEvent], object] | None = None
    minify_html: bool = False
    template_comments: bool = False
    _defaults: dict[str, Any] = field(init=False, repr=False, compare=False)

    def __post_init__(self) -> None:
        validate_chunk_size(self.chunk_size)
        for name in ('minify_html', 'template_comments'):
            if type(getattr(self, name)) is not bool:
                raise TypeError(f'{name} must be a boolean')

        if not isinstance(self.default_context, Mapping):
            raise TypeError('default_context must be a mapping with string keys')

        defaults = dict(self.default_context)
        if any(not isinstance(key, str) for key in defaults):
            raise TypeError('default_context must be a mapping with string keys')

        object.__setattr__(self, 'default_context', MappingProxyType(defaults))
        object.__setattr__(self, '_defaults', defaults)
        callback = self.on_render
        if callback is not None:
            if not callable(callback):
                raise TypeError('on_render must be callable')

            if any(
                check(function)
                for function in (callback, type(callback).__call__)
                for check in (iscoroutinefunction, isasyncgenfunction, isgeneratorfunction)
            ):
                raise TypeError('on_render must be synchronous and must not be a generator function')

    def context(self, context: Mapping[str, Any] | None) -> dict[str, Any]:
        values = self._defaults.copy()
        if context is not None:
            if type(context) is not dict and not isinstance(context, Mapping):
                raise TypeError('context must be a mapping with string keys or None')

            values.update(context)
            if any(not isinstance(key, str) for key in values):
                raise TypeError('context must be a mapping with string keys or None')

        return values

    def size(self, chunk_size: int | None) -> int:
        size = self.chunk_size if chunk_size is None else chunk_size
        validate_chunk_size(size)
        return size

@dataclass(frozen=True, eq=False)
class Template:
    """A reusable template handle returned by Engine.get_template().

    Compilation and helper imports wait until rendering, stream iteration or
    module access. Context mappings are shallow copies; their values stay shared.
    """

    name: str
    _resolve: Callable[[], _CompiledTemplate] = field(repr=False)
    _options: _RenderOptions = field(repr=False)

    @property
    def module(self) -> ModuleType:
        """Load or compile the generated Python module, importing its helpers."""
        return self._resolve().module

    def render(self, context: Mapping[str, Any] | None = None) -> str:
        """Render HTML with per-call values overriding the engine's defaults."""
        values = self._options.context(context)
        return self._resolve().render(values)

    def iter_render(
        self, context: Mapping[str, Any] | None = None, *, chunk_size: int | None = None,
    ) -> Generator[str]:
        """Snapshot context now and render lazily. Close the stream if stopping early.

        chunk_size is a buffering target in characters, not a maximum chunk length.
        None uses the engine's configured size.
        """
        size = self._options.size(chunk_size)
        values = self._options.context(context)
        return self._iter_render(values, size)

    async def render_async(self, context: Mapping[str, Any] | None = None) -> str:
        """Snapshot context when awaited, then compile and render in a worker thread."""
        values = self._options.context(context)
        return await to_thread(self._render, values)

    def iter_render_async(
        self, context: Mapping[str, Any] | None = None, *, chunk_size: int | None = None,
    ) -> AsyncGenerator[str]:
        """Snapshot context now and stream in worker threads; close with aclose()."""
        size = self._options.size(chunk_size)
        values = self._options.context(context)
        return _async_stream(partial(self._iter_render, values, size))

    def _render(self, values: Mapping[str, Any]) -> str:
        return self._resolve().render(values)

    def _iter_render(self, values: Mapping[str, Any], size: int) -> Generator[str]:
        yield from self._resolve().iter_render(values, chunk_size=size)

@dataclass(frozen=True)
class _CompiledTemplate:
    """Execute a loaded renderer with already validated context and options."""

    name: str
    module: ModuleType
    _options: _RenderOptions = field(default_factory=_RenderOptions, repr=False, compare=False)
    _comment: str = field(default='', repr=False, compare=False)
    _compiled_comment: str = field(default='', repr=False, compare=False)

    def render(self, values: Mapping[str, Any]) -> str:
        if self._options.on_render is None:
            result: str = self.module.render(values)
            return self._process(result)

        started = perf_counter()
        failure = None
        output_chars = 0
        try:
            result = self._process(self.module.render(values))
            output_chars = len(result)
            return result
        except BaseException as error:
            failure = error
            raise
        finally:
            self._notify(RenderEvent(
                self.name, perf_counter() - started, 'error' if failure is not None else 'completed',
                False, output_chars, failure,
            ))

    def iter_render(
        self, values: Mapping[str, Any], *, chunk_size: int,
    ) -> Generator[str]:
        chunks = self._stream(values, chunk_size)
        if self._options.on_render is None:
            return chunks

        return self._observe_stream(chunks)

    def _process(self, result: str) -> str:
        if self._options.minify_html:
            comment = self._compiled_comment
            if comment and result.endswith(comment):
                result = minify_html(result[:-len(comment)], self.name) + comment
            else:
                result = minify_html(result, self.name)

        return result + self._comment

    def _stream(self, values: Mapping[str, Any], size: int) -> Generator[str]:
        if self._options.minify_html:
            return self._minified_stream(values, size)

        chunks: Generator[str] = self.module.iter_render(values, chunk_size=size)
        return self._commented_stream(chunks, size) if self._comment else chunks

    def _minified_stream(self, values: Mapping[str, Any], size: int) -> Generator[str]:
        # Minifying separate chunks could change tags, entities or raw elements.
        result = self._process(self.module.render(values))
        for start in range(0, len(result), size):
            yield result[start : start + size]

    def _commented_stream(self, chunks: Generator[str], size: int) -> Generator[str]:
        yield from chunks
        for start in range(0, len(self._comment), size):
            yield self._comment[start : start + size]

    def _observe_stream(self, chunks: Generator[str]) -> Generator[str]:
        started = perf_counter()
        status: Literal['completed', 'closed', 'error'] = 'closed'
        failure = None
        output_chars = 0
        try:
            try:
                for chunk in chunks:
                    output_chars += len(chunk)
                    yield chunk

                status = 'completed'
            finally:
                chunks.close()
        except GeneratorExit:
            raise
        except BaseException as error:
            status = 'error'
            failure = error
            raise
        finally:
            self._notify(RenderEvent(self.name, perf_counter() - started, status, True, output_chars, failure))

    def _notify(self, event: RenderEvent) -> None:
        callback = self._options.on_render
        if callback is None:
            return

        try:
            result = ensure_sync(callback(event))
            if isgenerator(result):
                result.close()
                raise TypeError('on_render must not return a generator')

            if isasyncgen(result):
                raise TypeError('on_render must not return an async generator')
        except Exception:
            getLogger(__name__).exception('on_render callback failed for template %r', self.name)
