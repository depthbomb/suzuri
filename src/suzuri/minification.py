from __future__ import annotations
from dataclasses import replace, dataclass
from suzuri.html import _TAG, _BLOCK, _Minifier, _WHITESPACE
from suzuri.model import Loop, Node, Span, Text, Output, Scoped, Template, Conditional

@dataclass(frozen=True)
class _Hole:
    node: Output | Loop | Conditional | Scoped
    start: int
    end: int
    bodies: tuple[_Region, ...] = ()

@dataclass(frozen=True)
class _Region:
    start: int | None
    end: int | None
    holes: tuple[_Hole, ...]
    span: Span

class _TemplateMinifier:
    def __init__(self, template: Template) -> None:
        self.template = template
        self.parts: list[str] = []
        self.markers: list[str] = []
        self.block_markers: set[str] = set()
        self.prefix = '<!--!suzuri-compile:'
        literals = ''.join(_literals(template.body))
        while self.prefix in literals:
            self.prefix += ':'

    def run(self) -> tuple[Node, ...]:
        region = self._encode(self.template.body, bounded=False)
        compact = _Minifier(marker_prefix=self.prefix, block_markers=frozenset(self.block_markers)).run(''.join(self.parts))
        positions = []
        cursor = 0
        for marker in self.markers:
            start = compact.find(marker, cursor)
            if start < 0:
                # Unsupported markup may consume a marker as HTML syntax. Keep the AST intact.
                return self.template.body

            cursor = start + len(marker)
            positions.append((start, cursor))

        return self._decode(region, compact, positions)

    def _mark(self, *, block: bool = False) -> int:
        index = len(self.markers)
        marker = f'{self.prefix}{index}-->'
        self.markers.append(marker)
        self.parts.append(marker)
        if block:
            self.block_markers.add(marker)

        return index

    def _encode(self, nodes: tuple[Node, ...], *, bounded: bool = True) -> _Region:
        start = self._mark(block=_block_edge(nodes)) if bounded else None
        holes = []
        span = nodes[0].span if nodes else Span(self.template.source, 0, 0)
        for node in nodes:
            if isinstance(node, Text):
                self.parts.append(node.value)
                continue

            opening = self._mark(block=_block_edge((node,)))
            if isinstance(node, Output):
                holes.append(_Hole(node, opening, opening))
                continue

            bodies: tuple[_Region, ...]
            if isinstance(node, Loop):
                bodies = (self._encode(node.body),)
            elif isinstance(node, Scoped):
                bodies = (self._encode(node.template.body),)
            elif isinstance(node, Conditional):
                bodies = tuple(self._encode(body) for _, body in node.branches)
            else:
                raise ValueError('Includes must be expanded before HTML minification')

            holes.append(_Hole(node, opening, self._mark(block=_block_edge((node,), trailing=True)), bodies))

        end = self._mark(block=_block_edge(nodes, trailing=True)) if bounded else None
        return _Region(start, end, tuple(holes), span)

    def _decode(self, region: _Region, compact: str, positions: list[tuple[int, int]]) -> tuple[Node, ...]:
        cursor = positions[region.start][1] if region.start is not None else 0
        end = positions[region.end][0] if region.end is not None else len(compact)
        nodes: list[Node] = []
        for hole in region.holes:
            start = positions[hole.start][0]
            if start > cursor:
                nodes.append(Text(compact[cursor:start], region.span))

            node = hole.node
            if isinstance(node, Loop):
                node = replace(node, body=self._decode(hole.bodies[0], compact, positions))
            elif isinstance(node, Scoped):
                node = replace(node, template=replace(node.template, body=self._decode(hole.bodies[0], compact, positions)))
            elif isinstance(node, Conditional):
                node = replace(node, branches=tuple(
                    (condition, self._decode(body, compact, positions))
                    for (condition, _), body in zip(node.branches, hole.bodies, strict=True)
                ))

            nodes.append(node)
            cursor = positions[hole.end][1]

        if cursor < end:
            nodes.append(Text(compact[cursor:end], region.span))

        return tuple(nodes)

def _block_edge(nodes: tuple[Node, ...], *, trailing: bool = False) -> bool:
    """Only mark boundaries that emit a block tag on every possible render path."""
    for node in reversed(nodes) if trailing else nodes:
        if isinstance(node, Text):
            literal = node.value.strip(_WHITESPACE)
            if not literal:
                continue

            if trailing:
                tag = _TAG.fullmatch(literal, literal.rfind('<'))
            else:
                tag = _TAG.match(literal)

            return tag is not None and tag[2].lower() in _BLOCK

        if isinstance(node, Conditional) and node.branches[-1][0] is None:
            return all(_block_edge(body, trailing=trailing) for _, body in node.branches)

        if isinstance(node, Scoped):
            return _block_edge(node.template.body, trailing=trailing)

        # A loop or a condition without an else can be empty; outputs are unknown.
        return False

    return False

def _literals(nodes: tuple[Node, ...]) -> list[str]:
    values = []
    for node in nodes:
        if isinstance(node, Text):
            values.append(node.value)
        elif isinstance(node, Loop):
            values.extend(_literals(node.body))
        elif isinstance(node, Scoped):
            values.extend(_literals(node.template.body))
        elif isinstance(node, Conditional):
            for _, body in node.branches:
                values.extend(_literals(body))

    return values

def minify_template(template: Template) -> tuple[Node, ...]:
    """Minify static HTML while retaining directives, original expressions and spans.

    Preserved comments mark dynamic boundaries for the HTML scanner. These markers
    are removed when rebuilding the AST; uncertain boundary whitespace stays intact.
    """
    return _TemplateMinifier(template).run()
