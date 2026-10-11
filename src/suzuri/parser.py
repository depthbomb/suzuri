from __future__ import annotations
from ast import Call, walk
from suzuri.expressions import valid_name, parse_expression
from suzuri.lexer import Token, tokenize, VOID_ELEMENTS, directive_attributes
from suzuri.model import Expr, Loop, Node, Text, Import, Output, Source, Include, Template, Attribute, Component, Parameter, Conditional

_RULES = {
    'out': ({'value'}, set()),
    'if': ({'test'}, set()),
    'elif': ({'test'}, set()),
    'else': (set(), set()),
    'for': ({'each', 'in'}, set()),
    'from': ({'module', 'import'}, {'as'}),
    'import': ({'module', 'as'}, set()),
    'param': ({'name', 'type'}, set()),
    'include': ({'src'}, {'with'}),
    'component': ({'name'}, set()),
}
_SINGLE = frozenset({'out', 'from', 'import', 'param', 'include', 'component'})

class Parser:
    def __init__(self, source: Source) -> None:
        if len(source.text) > 2_000_000:
            raise source.error('Template exceeds the 2,000,000 character limit', 0)

        self.source = source
        self.tokens = list(tokenize(source))
        self.index = 0
        self.html: list[tuple[str, int]] = []
        self.imports: list[Import] = []
        self.parameters: list[Parameter] = []
        self.bindings: set[str] = set()

    def parse(self) -> Template:
        body = self._body()
        if self.html:
            name, offset = self.html[-1]
            raise self.source.error(f'Unclosed <{name}> element', offset)

        return Template(self.source, body, tuple(self.imports), tuple(self.parameters))

    def _attributes(self, token: Token) -> dict[str, Attribute]:
        attributes = directive_attributes(token)
        required, optional = _RULES[token.name]
        missing = required - attributes.keys()
        unknown = attributes.keys() - required - optional if token.name != 'component' else set()
        if missing or unknown:
            detail = f'missing {sorted(missing)}' if missing else f'unknown {sorted(unknown)}'
            raise self.source.error(f'<{token.name}>: {detail} attributes', token.span.start)

        if token.self_closing != (token.name in _SINGLE):
            expected = 'a self-closing tag' if token.name in _SINGLE else 'an opening and closing tag'
            raise self.source.error(f'<{token.name}> requires {expected}', token.span.start)

        return attributes

    def _binding(self, attribute: Attribute) -> str:
        name = attribute.value
        if not valid_name(name):
            raise self.source.error(f'Invalid or reserved Python name {name!r}', attribute.span.start)

        return name

    def _declaration(self, token: Token, attributes: dict[str, Attribute]) -> None:
        if token.name == 'param':
            name = self._binding(attributes['name'])
            annotation = parse_expression(attributes['type'])
            if any(isinstance(node, Call) for node in walk(annotation.tree)):
                raise self.source.error('Type annotations cannot contain calls', annotation.span.start)

            self.parameters.append(Parameter(name, annotation, token.span))
        else:
            module = attributes['module'].value
            if not module or module == '__future__' or not all(valid_name(part) for part in module.split('.')):
                raise self.source.error(
                    'Imports require an absolute Python module name', attributes['module'].span.start
                )

            imported = self._binding(attributes['import']) if token.name == 'from' else None
            if 'as' in attributes:
                name = self._binding(attributes['as'])
            else:
                assert imported is not None
                name = imported
            self.imports.append(Import(module, imported, name, token.span))

        if name in self.bindings:
            raise self.source.error(f'Duplicate declaration {name!r}', token.span.start)

        self.bindings.add(name)

    def _body(self, closing: str | None = None, depth: int = 0) -> tuple[Node, ...]:
        if depth > 64:
            raise self.source.error('Directive nesting exceeds 64 levels', self.tokens[self.index - 1].span.start)

        entry_html = self.html.copy()
        nodes: list[Node] = []
        while self.index < len(self.tokens):
            token = self.tokens[self.index]
            self.index += 1
            if token.kind != 'directive':
                self._literal(token)
                nodes.append(Text(token.text, token.span))
                continue

            if token.closing:
                if token.name != closing:
                    raise self.source.error(f'Unexpected </{token.name}>', token.span.start)

                if self.html != entry_html:
                    raise self.source.error(
                        'Directive blocks must preserve their HTML element context', token.span.start
                    )

                return tuple(nodes)

            if any(name in {'svg', 'math'} for name, _ in self.html):
                raise self.source.error('Directives inside SVG or MathML are unsupported', token.span.start)

            attributes = self._attributes(token)
            if token.name in {'from', 'import', 'param'}:
                if closing or self.html or any(not isinstance(node, Text) or node.value.strip() for node in nodes):
                    raise self.source.error(
                        'Declarations must precede rendering content at the top level', token.span.start
                    )

                self._declaration(token, attributes)
                nodes.clear()
            elif token.name == 'out':
                nodes.append(Output(parse_expression(attributes['value']), token.span))
            elif token.name == 'include':
                data = parse_expression(attributes['with']) if 'with' in attributes else None
                nodes.append(Include(attributes['src'].value, token.span, data))
            elif token.name == 'component':
                props: dict[str, Expr | str] = {}
                for key, attribute in attributes.items():
                    if key == 'name':
                        continue

                    prop = key.removeprefix(':')
                    if prop == 'name' or not valid_name(prop):
                        raise self.source.error(f'Invalid or reserved component input {prop!r}', attribute.span.start)

                    if prop in props:
                        raise self.source.error(f'Duplicate component input {prop!r}', attribute.span.start)

                    props[prop] = parse_expression(attribute) if key.startswith(':') else attribute.value

                nodes.append(Component(attributes['name'].value, tuple(props.items()), token.span))
            elif token.name == 'for':
                name = self._binding(attributes['each'])
                iterable = parse_expression(attributes['in'])
                nodes.append(Loop(name, iterable, self._body('for', depth + 1), token.span))
            elif token.name == 'if':
                branches: list[tuple[Expr | None, tuple[Node, ...]]] = [
                    (parse_expression(attributes['test']), self._body('if', depth + 1))
                ]
                while self.index < len(self.tokens):
                    candidate = self.index
                    while (
                        candidate < len(self.tokens)
                        and self.tokens[candidate].kind == 'text'
                        and not self.tokens[candidate].text.strip()
                    ):
                        candidate += 1

                    if candidate == len(self.tokens):
                        break

                    branch = self.tokens[candidate]
                    if branch.kind != 'directive' or branch.closing or branch.name not in {'elif', 'else'}:
                        break

                    self.index = candidate + 1
                    branch_attributes = self._attributes(branch)
                    condition = parse_expression(branch_attributes['test']) if branch.name == 'elif' else None
                    branches.append((condition, self._body(branch.name, depth + 1)))
                    if condition is None:
                        break

                nodes.append(Conditional(tuple(branches), token.span))
            else:
                raise self.source.error(f'Orphan <{token.name}> branch', token.span.start)

        if closing:
            raise self.source.error(f'Unclosed <{closing}> directive', len(self.source.text))

        return tuple(nodes)

    def _literal(self, token: Token) -> None:
        if token.kind == 'text' and token.text.endswith('<') and (
            self.index == len(self.tokens) or self.tokens[self.index].kind == 'directive'
        ):
            raise self.source.error('Use &lt; for a literal < at a template boundary', token.span.end - 1)

        foreign = any(name in {'svg', 'math'} for name, _ in self.html)
        if token.kind == 'cdata' and not foreign:
            raise self.source.error('CDATA is supported only in static SVG or MathML', token.span.start)

        if token.kind != 'html':
            return

        if token.closing:
            if not self.html or self.html[-1][0] != token.name:
                raise self.source.error(f'Mismatched </{token.name}> element', token.span.start)

            self.html.pop()
        elif token.name not in VOID_ELEMENTS:
            if token.self_closing and not foreign and token.name not in {'svg', 'math'}:
                raise self.source.error(f'<{token.name}> requires an explicit end tag', token.span.start)

            if not token.self_closing:
                self.html.append((token.name, token.span.start))

            if len(self.html) > 128:
                raise self.source.error('HTML nesting exceeds 128 levels', token.span.start)
