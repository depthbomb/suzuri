from __future__ import annotations
from typing import Any
from html import escape
from sys import version_info
from suzuri.errors import ArtifactError
from inspect import isawaitable, iscoroutine
from collections.abc import Mapping, Iterable, Generator

VERSION = '0.1.0'
ABI_VERSION = 1
COMPILER_REVISION = 3
ESCAPING_POLICY = 'html-text-escape-all-v1'
PYTHON_TARGET = (3, 15) if version_info >= (3, 15) else (3, 14)
PYTHON_REQUIRES = f'>={PYTHON_TARGET[0]}.{PYTHON_TARGET[1]}'

def require_python(requirement: str) -> None:
    if requirement not in ('>=3.14', '>=3.15'):
        raise ArtifactError('Unsupported Python target; rebuild the bundle with a supported Python version')

    if requirement == '>=3.15' and version_info < (3, 15):
        raise ArtifactError('This bundle requires Python 3.15 or newer; rebuild it with Python 3.14 to run here')

def require_abi(version: int, compiler_revision: int = 1) -> None:
    if type(version) is not int or version != ABI_VERSION:
        raise ArtifactError(
            f'Artifact ABI {version} is incompatible with runtime ABI {ABI_VERSION}; rebuild the bundle'
        )

    if type(compiler_revision) is not int or compiler_revision != COMPILER_REVISION:
        raise ArtifactError('Incompatible compiler revision; rebuild the bundle')

def ensure_sync(value: object) -> object:
    kind = type(value)
    if kind is str or kind is int or kind is bool or kind is float or value is None:
        return value

    if isawaitable(value):
        if iscoroutine(value):
            value.close()

        raise TypeError('Awaitable values are unsupported by synchronous templates')

    return value

def escape_text(value: object) -> str:
    """Escape all values, including objects exposing __html__, as ordinary text."""
    if type(value) is str:
        text = value
    else:
        kind = type(value)
        if kind is int or kind is bool or kind is float or value is None:
            return str(value)

        text = str(ensure_sync(value))
        # __str__ may return a subclass with an overridden replace or __contains__.
        if type(text) is not str:
            text = str.__str__(text)

    if '&' not in text and '<' not in text and '>' not in text and '"' not in text and "'" not in text:
        return text

    return escape(text, quote=True)

def truth(value: object) -> bool:
    return bool(ensure_sync(value))

def scoped_context(parent: Mapping[str, Any], bindings: Mapping[str, Any], data: object) -> dict[str, Any]:
    values = ensure_sync(data)
    if not isinstance(values, Mapping):
        raise TypeError('Include data must be a mapping with string keys')

    result = dict(parent)
    result.update(bindings)
    for name, value in values.items():
        if not isinstance(name, str):
            raise TypeError('Include data must be a mapping with string keys')

        result[name] = value

    return result

def coalesce(chunks: Iterable[str], size: int = 8192) -> Generator[str]:
    """Coalesce small chunks and close the underlying generator on cancellation."""
    if size < 1:
        raise ValueError('chunk_size must be positive')

    iterator = iter(chunks)
    parts: list[str] = []
    length = 0
    try:
        for chunk in iterator:
            parts.append(chunk)
            length += len(chunk)
            if length >= size:
                yield ''.join(parts)
                parts.clear()
                length = 0

        if parts:
            yield ''.join(parts)
    finally:
        close = getattr(iterator, 'close', None)
        if close is not None:
            close()
