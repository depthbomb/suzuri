from __future__ import annotations
import builtins
from suzuri.expressions import character_offset, parse_expression
from ast import Dict, Name, walk, Store, SetComp, Constant, DictComp, ListComp, NodeVisitor, GeneratorExp
from suzuri.model import Expr, Loop, Node, Span, Import, Output, Scoped, Include, Template, Attribute, Component, Conditional

type _ResolutionKey = tuple[int, tuple[tuple[str, str], ...], str]
type _Resolution = tuple[str, tuple[int, ...]]

def scoped_values(node: Scoped) -> dict[str, Expr | str] | None:
    if node.data is None:
        return dict(node.props)

    literal = node.data.tree.body
    if not isinstance(literal, Dict) or any(not isinstance(key, Constant) or not isinstance(key.value, str) for key in literal.keys):
        return None

    values: dict[str, Expr | str] = {}
    for key, value in zip(literal.keys, literal.values, strict=True):
        assert isinstance(key, Constant) and isinstance(key.value, str)
        start = character_offset(node.data, value)
        end = character_offset(node.data, value, end=True)
        offsets = node.data.offsets[start:end + 1]
        values[key.value] = parse_expression(Attribute(
            '(' + node.data.text[start:end] + '\n)', Span(node.data.span.source, offsets[0], offsets[-1]),
            (offsets[0], *offsets, offsets[-1], offsets[-1]),
        ))

    return values

class Resolver(NodeVisitor):
    def __init__(
        self, expression: Expr, names: dict[str, str], imports: dict[str, str], parameters: set[str], mode: str,
        context_name: str = '_suzuri_context',
        context_builtins: bool = False,
    ) -> None:
        self.expression = expression
        self.names = names
        self.imports = imports
        self.parameters = parameters
        self.mode = mode
        self.context_name = context_name
        self.context_builtins = context_builtins
        self.bound: set[str] = set()
        self.context: set[str] = set()
        self.replacements = list(expression.debug_replacements)

    def visit_Name(self, node: Name) -> None:
        if node.id in self.bound or isinstance(node.ctx, Store):
            return

        replacement = node.id
        if node.id in self.names:
            replacement = self.names[node.id]
        elif node.id in self.imports:
            replacement = self.imports[node.id]
        elif self.mode != 'annotation' and (
            node.id in self.parameters or node.id in ('sentinel', 'frozendict') or not hasattr(builtins, node.id)
        ):
            self.context.add(node.id)
            if self.mode == 'runtime':
                replacement = f'{self.context_name}[{node.id!r}]'
        elif self.context_builtins and self.mode == 'runtime':
            replacement = f'{self.context_name}.get({node.id!r}, {node.id})'

        if replacement != node.id:
            self.replacements.append(
                (
                    character_offset(self.expression, node),
                    character_offset(self.expression, node, end=True),
                    replacement,
                )
            )

    def visit_ListComp(self, node: ListComp | SetComp | DictComp | GeneratorExp) -> None:
        previous = self.bound.copy()
        for generator in node.generators:
            if generator.is_async:
                raise self.expression.span.source.error(
                    'Async comprehensions are unsupported', self.expression.span.start
                )

        # Only the first iterable runs in the surrounding scope. Every target
        # is local throughout the rest of the comprehension, even before binding.
        self.visit(node.generators[0].iter)
        self.bound.update(
            item.id for generator in node.generators for item in walk(generator.target)
            if isinstance(item, Name) and isinstance(item.ctx, Store)
        )
        for index, generator in enumerate(node.generators):
            if index:
                self.visit(generator.iter)

            self.visit(generator.target)
            for condition in generator.ifs:
                self.visit(condition)

        if isinstance(node, DictComp):
            self.visit(node.key)
            if node.value is not None:
                self.visit(node.value)
        else:
            self.visit(node.elt)

        self.bound = previous

    visit_SetComp = visit_ListComp
    visit_DictComp = visit_ListComp
    visit_GeneratorExp = visit_ListComp

    def resolve(self) -> tuple[str, tuple[int, ...]]:
        self.visit(self.expression.tree)
        chunks: list[str] = []
        offsets: list[int] = []
        cursor = 0
        for start, end, replacement in sorted(self.replacements):
            chunks.append(self.expression.text[cursor:start])
            offsets.extend(self.expression.offsets[cursor:start])
            chunks.append(replacement)
            offsets.extend([self.expression.offsets[start]] * len(replacement))
            cursor = end

        chunks.append(self.expression.text[cursor:])
        offsets.extend(self.expression.offsets[cursor:])
        return ''.join(chunks), tuple(offsets)

class Analysis:
    def __init__(
        self, template: Template, *, prefix: str = '_suzuri', inputs: set[str] | None = None,
        context_builtins: bool = False, loop_depth: int = 0,
    ) -> None:
        self.template = template
        self.prefix = prefix
        self.context_name = prefix + '_context'
        self.context_builtins = context_builtins
        self.imports = {item.alias: f'{prefix}_import_{i}' for i, item in enumerate(template.imports)}
        self.checker_imports = {name: name if prefix == '_suzuri' else alias for name, alias in self.imports.items()}
        self.parameters = {item.name: item.annotation for item in template.parameters}
        self.inputs = set(self.parameters) | (inputs or set())
        self.context = set(self.parameters)
        self.required = set(self.parameters)
        self.loops: dict[int, str] = {}
        self.scopes: dict[int, Analysis] = {}
        self.values: dict[int, dict[str, Expr | str] | None] = {}
        self._resolved: dict[_ResolutionKey, _Resolution] = {}
        self._visit(template.body, {}, loop_depth)

    @property
    def optional(self) -> set[str]:
        return self.context - self.required

    def all_imports(self) -> tuple[Import, ...]:
        imports = list(self.template.imports)
        for child in self.scopes.values():
            imports.extend(child.all_imports())

        return tuple(imports)

    def annotation(self, name: str) -> str:
        annotation = self.parameters.get(name)
        if annotation is None:
            return '_suzuri_Any'

        return Resolver(annotation, {}, self.checker_imports, set(), 'annotation').resolve()[0]

    def reference(self, name: str, scope: dict[str, str], mode: str) -> str:
        if name in scope:
            return scope[name]

        imports = self.imports if mode == 'runtime' else self.checker_imports
        if name in imports:
            return imports[name]

        return f'{self.context_name}[{name!r}]' if mode == 'runtime' else name

    def resolve(self, expression: Expr, scope: dict[str, str], mode: str) -> tuple[str, tuple[int, ...]]:
        key = (id(expression), tuple(scope.items()), mode)
        cached = self._resolved.get(key)
        if cached is not None:
            return cached

        imports = self.imports if mode == 'runtime' else self.checker_imports
        resolver = Resolver(expression, scope, imports, self.inputs, mode, self.context_name, self.context_builtins)
        result = resolver.resolve()
        self.context.update(resolver.context)
        self.required.update(resolver.context)
        self._resolved[key] = result
        return result

    def _visit(self, nodes: tuple[Node, ...], scope: dict[str, str], loop_depth: int) -> None:
        for node in nodes:
            if isinstance(node, Output):
                self.resolve(node.expression, scope, 'runtime')
            elif isinstance(node, Loop):
                if loop_depth >= 19:
                    # CPython allows 20 static blocks; the renderer needs one for try.
                    raise node.span.source.error('Loop nesting exceeds 19 levels', node.span.start)

                if node.name in self.imports:
                    raise node.span.source.error('Loop bindings cannot shadow imported names', node.span.start)

                self.resolve(node.iterable, scope, 'runtime')
                name = f'{self.prefix}_loop_{len(self.loops)}'
                self.loops[id(node)] = name
                self._visit(node.body, {**scope, node.name: name}, loop_depth + 1)
            elif isinstance(node, Conditional):
                for condition, body in node.branches:
                    if condition is not None:
                        self.resolve(condition, scope, 'runtime')

                    self._visit(body, scope, loop_depth)
            elif isinstance(node, Scoped):
                if node.data is not None:
                    self.resolve(node.data, scope, 'runtime')

                for _, value in node.props:
                    if isinstance(value, Expr):
                        self.resolve(value, scope, 'runtime')

                values = scoped_values(node)
                inherited = set(scope) | set(self.imports) | self.inputs if node.inherit else set()
                child = Analysis(
                    node.template, prefix=f'{self.prefix}_view_{len(self.scopes)}',
                    inputs=inherited | (set(values) if values is not None else set()),
                    context_builtins=node.inherit,
                    loop_depth=loop_depth,
                )
                self.scopes[id(node)] = child
                self.values[id(node)] = values
                if node.inherit:
                    for name in child.context:
                        if name in scope or name in self.imports or values is not None and name in values:
                            continue

                        self.context.add(name)
                        if values is not None and name in child.required:
                            self.required.add(name)
                else:
                    missing = child.required - dict(node.props).keys()
                    if missing:
                        raise node.span.source.error(f'Missing component inputs: {sorted(missing)}', node.span.start)
            elif isinstance(node, (Include, Component)):
                raise node.span.source.error(
                    'Includes and components require Engine with a template source directory or build()', node.span.start
                )
