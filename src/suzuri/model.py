from __future__ import annotations
from re import finditer
from ast import Expression
from bisect import bisect_right
from functools import cached_property
from dataclasses import field, dataclass
from suzuri.errors import TemplateSyntaxError

@dataclass(frozen=True)
class Source:
    text: str
    filename: str
    lines: tuple[int, ...] = field(init=False)

    def __post_init__(self) -> None:
        object.__setattr__(self, 'lines', (0, *(match.end() for match in finditer('\n', self.text))))

    def location(self, offset: int) -> tuple[int, int]:
        line = bisect_right(self.lines, offset)
        return line, offset - self.lines[line - 1] + 1

    def error(self, message: str, offset: int) -> TemplateSyntaxError:
        return TemplateSyntaxError(message, self.filename, *self.location(offset))

@dataclass(frozen=True)
class Span:
    source: Source
    start: int
    end: int

    def location(self) -> tuple[str, int, int]:
        return self.source.filename, *self.source.location(self.start)

    def as_dict(self) -> dict[str, str | int]:
        line, column = self.source.location(self.start)
        return {'file': self.source.filename, 'start': self.start, 'end': self.end, 'line': line, 'column': column}

@dataclass(frozen=True)
class Attribute:
    value: str
    span: Span
    offsets: tuple[int, ...]

@dataclass(frozen=True)
class Expr:
    text: str
    tree: Expression
    span: Span
    offsets: tuple[int, ...]
    debug_replacements: tuple[tuple[int, int, str], ...] = ()

    @cached_property
    def line_starts(self) -> tuple[int, ...]:
        return (0, *(match.end() for match in finditer('\n', self.text)))

    @cached_property
    def utf8_lines(self) -> tuple[bytes, ...]:
        return tuple(line.encode('utf-8') for line in self.text.split('\n'))

@dataclass(frozen=True)
class Text:
    value: str
    span: Span

@dataclass(frozen=True)
class Output:
    expression: Expr
    span: Span

@dataclass(frozen=True)
class Loop:
    name: str
    iterable: Expr
    body: tuple[Node, ...]
    span: Span

@dataclass(frozen=True)
class Conditional:
    branches: tuple[tuple[Expr | None, tuple[Node, ...]], ...]
    span: Span

@dataclass(frozen=True)
class Include:
    path: str
    span: Span
    data: Expr | None = None

@dataclass(frozen=True)
class Component:
    name: str
    props: tuple[tuple[str, Expr | str], ...]
    span: Span

@dataclass(frozen=True)
class Scoped:
    template: Template
    data: Expr | None
    props: tuple[tuple[str, Expr | str], ...]
    inherit: bool
    span: Span

@dataclass(frozen=True)
class Import:
    module: str
    name: str | None
    alias: str
    span: Span

@dataclass(frozen=True)
class Parameter:
    name: str
    annotation: Expr
    span: Span

Node = Text | Output | Loop | Conditional | Include | Component | Scoped

@dataclass(frozen=True)
class Template:
    source: Source
    body: tuple[Node, ...]
    imports: tuple[Import, ...]
    parameters: tuple[Parameter, ...]
