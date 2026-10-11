from __future__ import annotations
from hashlib import sha256
from suzuri.parser import Parser
from collections.abc import Mapping
from suzuri.analysis import Analysis
from suzuri.html import template_comment
from dataclasses import replace, dataclass
from suzuri.optimizer import optimize_nodes
from suzuri.minification import minify_template
from suzuri._validation import validate_compile_options
from suzuri.model import Expr, Loop, Node, Span, Text, Output, Scoped, Source, Template, Conditional
from suzuri.runtime import VERSION, ABI_VERSION, PYTHON_REQUIRES, ESCAPING_POLICY, COMPILER_REVISION

@dataclass(frozen=True)
class Compilation:
    """Generated source artifacts. Use build() to publish a complete on-disk bundle."""

    code: str
    stub: str
    checker: str
    source_map: dict[str, object]
    context_names: tuple[str, ...]
    imports: tuple[dict[str, str | None], ...]
    python_requires: str = PYTHON_REQUIRES

class Writer:
    def __init__(self, analysis: Analysis, mode: str) -> None:
        self.analysis = analysis
        self.mode = mode
        self.parts: list[str] = []
        self.mappings: list[dict[str, object]] = []
        self.indent = 0
        self.length = 0

    def line(self, value: str = '') -> None:
        line = '    ' * self.indent + value + '\n' if value else '\n'
        self.parts.append(line)
        self.length += len(line)

    def expression(self, prefix: str, expression: Expr, suffix: str, scope: dict[str, str]) -> None:
        value, offsets = self.analysis.resolve(expression, scope, self.mode)
        start = self.length + self.indent * 4 + len(prefix) + 1
        self.mappings.append(
            {
                'kind': 'expression',
                'generated_start': start,
                'generated_end': start + len(value),
                'source': expression.span.as_dict(),
                'source_offsets': offsets,
            }
        )
        terminator = '\n)' if '#' in value else ')'
        self.line(prefix + '(' + value + terminator + suffix)

    def body(self, nodes: tuple[Node, ...], scope: dict[str, str], *, streaming: bool = False) -> None:
        if not nodes:
            self.line('pass')

        for node in nodes:
            if isinstance(node, Text):
                if self.mode == 'checker':
                    self.line('pass')
                else:
                    self.line(f'yield {node.value!r}' if streaming else f'_suzuri_append({node.value!r})')
            elif isinstance(node, Output):
                self._location(node)
                prefix = (
                    ''
                    if self.mode == 'checker'
                    else 'yield _suzuri_escape'
                    if streaming
                    else '_suzuri_append(_suzuri_escape'
                )
                suffix = ')' if self.mode != 'checker' and not streaming else ''
                self.expression(prefix, node.expression, suffix, scope)
            elif isinstance(node, Loop):
                self._location(node)
                name = self.analysis.loops[id(node)]
                prefix = f'for {name} in ' if self.mode == 'checker' else f'for {name} in _suzuri_sync'
                self.expression(prefix, node.iterable, ':', scope)
                self.indent += 1
                self.body(node.body, {**scope, node.name: name}, streaming=streaming)
                self._location(node)
                self.indent -= 1
            elif isinstance(node, Conditional):
                self._branches(node, scope, streaming)
            elif isinstance(node, Scoped):
                self._scope(node, scope, streaming)

    def _scope(self, node: Scoped, scope: dict[str, str], streaming: bool) -> None:
        parent = self.analysis
        child = parent.scopes[id(node)]
        values = parent.values[id(node)]
        data_name = child.prefix + '_data'
        self._location(node)
        if node.data is not None:
            annotation = ': _suzuri_Any' if self.mode == 'checker' else ''
            self.expression(data_name + annotation + ' = ', node.data, '', scope)
        else:
            entries = []
            for index, (name, value) in enumerate(node.props):
                if isinstance(value, Expr):
                    variable = child.prefix + f'_prop_{index}'
                    if self.mode == 'runtime':
                        self.line(f'_suzuri_location = {value.span.location()!r}')

                    prefix = variable + (' = _suzuri_sync' if self.mode == 'runtime' else ' = ')
                    self.expression(prefix, value, '', scope)
                else:
                    variable = repr(value)

                entries.append(f'{name!r}: {variable}')

            self.line(data_name + ' = {' + ', '.join(entries) + '}')

        local_scope = {}
        if self.mode == 'runtime':
            self._location(node)
            inherited = parent.context_name if node.inherit else '{}'
            bindings = {**parent.imports, **scope} if node.inherit else {}
            locals_text = '{' + ', '.join(f'{name!r}: {value}' for name, value in bindings.items()) + '}'
            if node.inherit:
                self.line(f'{child.context_name} = _suzuri_scoped_context({inherited}, {locals_text}, {data_name})')
            else:
                self.line(f'{child.context_name} = {data_name}')
        else:
            for index, name in enumerate(sorted(child.context)):
                local = child.prefix + f'_input_{index}'
                local_scope[name] = local
                annotation = child.annotation(name)
                terminator = '\n)' if '#' in annotation else ')'
                prefix = f'{local}: ({annotation}{terminator} = '
                if values is not None and name in values:
                    supplied = values[name]
                    if isinstance(supplied, Expr):
                        self.expression(prefix, supplied, '', scope)
                    else:
                        self.line(prefix + repr(supplied))
                else:
                    fallback = parent.reference(name, scope, self.mode) if node.inherit else '_suzuri_missing'
                    value = f'{data_name}.get({name!r}, {fallback})' if values is None else fallback
                    self.line(prefix + value)

        self.analysis = child
        self.body(node.template.body, local_scope, streaming=streaming)
        self.analysis = parent

    def _branches(self, node: Conditional, scope: dict[str, str], streaming: bool) -> None:
        for index, (condition, body) in enumerate(node.branches):
            if condition is None:
                self.line('else:')
            else:
                keyword = 'if' if index == 0 else 'elif'
                if self.mode == 'checker':
                    prefix, suffix = keyword + ' ', ':'
                else:
                    # Set the error location before evaluating each condition.
                    location = condition.span.location()
                    prefix = f'{keyword} _suzuri_truth((_suzuri_location := {location!r}) and '
                    suffix = '):'

                self.expression(prefix, condition, suffix, scope)

            self.indent += 1
            self.body(body, scope, streaming=streaming)
            self.indent -= 1

    def _location(self, node: Node) -> None:
        if self.mode != 'checker':
            self.line(f'_suzuri_location = {node.span.location()!r}')

    def finish(self) -> str:
        return ''.join(self.parts)

def _imports(writer: Writer, template: Template, *, runtime: bool, analysis: Analysis | None = None) -> None:
    analysis = writer.analysis if analysis is None else analysis
    for item in template.imports:
        alias = analysis.imports[item.alias] if runtime else analysis.checker_imports[item.alias]
        statement = (
            f'from {item.module} import {item.name} as {alias}' if item.name else f'import {item.module} as {alias}'
        )
        start = writer.length
        writer.line(statement)
        writer.mappings.append(
            {
                'kind': 'import',
                'generated_start': start,
                'generated_end': writer.length - 1,
                'source': item.span.as_dict(),
                'module': item.module,
                'name': item.name,
                'alias': item.alias,
            }
        )

    for child in analysis.scopes.values():
        _imports(writer, child.template, runtime=runtime, analysis=child)

def _signature(analysis: Analysis, *, annotated: bool, stub: bool = False) -> str:
    parameters = []
    for name in sorted(analysis.context):
        text = analysis.annotation(name)
        terminator = '\n)' if '#' in text else ')'
        parameter = f'{name}: ({text}{terminator}' if annotated else name
        if name in analysis.optional:
            parameter += ' = ...' if stub else ' = _suzuri_missing'

        parameters.append(parameter)

    return '*, ' + ', '.join(parameters) if parameters else ''

def _runtime(
    template: Template, analysis: Analysis, interface: Analysis, *, minified: bool = False, comment: str = '',
) -> Writer:
    writer = Writer(analysis, 'runtime')
    writer.line('from __future__ import annotations')
    writer.line('from suzuri.errors import TemplateRenderError as _suzuri_RenderError')
    writer.line('from suzuri.runtime import require_python as _suzuri_require_python')
    writer.line('from suzuri.runtime import require_abi as _suzuri_require, escape_text as _suzuri_escape')
    writer.line(
        'from suzuri.runtime import truth as _suzuri_truth, ensure_sync as _suzuri_sync, coalesce as _suzuri_coalesce'
    )
    if analysis.scopes:
        writer.line('from suzuri.runtime import scoped_context as _suzuri_scoped_context')

    if interface.optional:
        writer.line('_suzuri_missing = object()')

    writer.line(f'_suzuri_require({ABI_VERSION}, {COMPILER_REVISION})')
    writer.line(f'__suzuri_python_requires__ = {PYTHON_REQUIRES!r}')
    writer.line('_suzuri_require_python(__suzuri_python_requires__)')
    if minified:
        writer.line('__suzuri_minified__ = True')

    if comment:
        writer.line(f'__suzuri_template_comment__ = {comment!r}')

    _imports(writer, template, runtime=True)
    writer.line()
    for streaming in (False, True):
        name = '_suzuri_iter' if streaming else 'render'
        writer.line(f'def {name}(_suzuri_context):')
        writer.indent = 1
        if not streaming and all(isinstance(node, Text) for node in template.body):
            writer.line('return ' + repr(''.join(node.value for node in template.body if isinstance(node, Text))))
        else:
            if streaming:
                writer.line('yield from ()')
            else:
                writer.line('_suzuri_parts = []')
                writer.line('_suzuri_append = _suzuri_parts.append')

            writer.line(f'_suzuri_location = {(template.source.filename, 1, 1)!r}')
            writer.line('try:')
            writer.indent = 2
            writer.body(template.body, {}, streaming=streaming)
            writer.indent = 1
            writer.line('except Exception as _suzuri_error:')
            writer.indent = 2
            writer.line('raise _suzuri_RenderError(str(_suzuri_error), *_suzuri_location) from _suzuri_error')
            writer.indent = 1
            if not streaming:
                writer.line("return ''.join(_suzuri_parts)")

        writer.indent = 0
        writer.line()

    writer.line('def iter_render(context, *, chunk_size=8192):')
    writer.indent = 1
    writer.line('return _suzuri_coalesce(_suzuri_iter(context), chunk_size)')
    writer.indent = 0
    writer.line()
    writer.line(f'def render_values({_signature(interface, annotated=False)}):')
    writer.indent = 1
    context = '{' + ', '.join(f'{name!r}: {name}' for name in sorted(interface.context)) + '}'
    if interface.optional:
        writer.line(f'_suzuri_values = {context}')
        writer.line('return render({key: value for key, value in _suzuri_values.items() if value is not _suzuri_missing})')
    else:
        writer.line(f'return render({context})')
    return writer

def _checker(template: Template, analysis: Analysis) -> Writer:
    writer = Writer(analysis, 'checker')
    writer.line('from __future__ import annotations')
    writer.line('from typing import Any as _suzuri_Any')
    _imports(writer, template, runtime=False)
    if analysis.optional or analysis.scopes:
        writer.line('_suzuri_missing: _suzuri_Any = object()')

    writer.line()
    writer.line(f'def _suzuri_check({_signature(analysis, annotated=True)}) -> None:')
    writer.indent = 1
    writer.body(template.body, {})
    return writer

def _stub(template: Template, analysis: Analysis) -> str:
    writer = Writer(analysis, 'checker')
    writer.line('from __future__ import annotations')
    writer.line('from typing import Any as _suzuri_Any, TypedDict as _suzuri_TypedDict')
    if analysis.optional:
        writer.line('from typing import NotRequired as _suzuri_NotRequired')

    writer.line('from collections.abc import Generator as _suzuri_Generator')
    _imports(writer, template, runtime=False)
    writer.line()
    writer.line('class _suzuri_Context(_suzuri_TypedDict):')
    writer.indent = 1
    for name in sorted(analysis.context):
        text = analysis.annotation(name)
        terminator = '\n)' if '#' in text else ')'
        value = f'({text}{terminator}'
        if name in analysis.optional:
            value = f'_suzuri_NotRequired[{value}]'

        writer.line(f'{name}: {value}')

    if not analysis.context:
        writer.line('pass')

    writer.indent = 0
    writer.line()
    writer.line('def render(context: _suzuri_Context) -> str: ...')
    writer.line('def iter_render(context: _suzuri_Context, *, chunk_size: int = ...) -> _suzuri_Generator[str]: ...')
    writer.line(f'def render_values({_signature(analysis, annotated=True, stub=True)}) -> str: ...')
    return writer.finish()

def compile_parsed(
    template: Template, *, optimize: bool = True, minify_html: bool = False, template_comments: bool = False,
    metadata: Mapping[str, object] | None = None,
) -> Compilation:
    validate_compile_options(optimize=optimize, minify_html=minify_html, template_comments=template_comments)

    interface = Analysis(template)
    checker = _checker(template, interface)
    optimized = replace(template, body=optimize_nodes(template.body, fold_outputs=not minify_html)) if optimize else template
    if minify_html:
        optimized = replace(optimized, body=minify_template(optimized))
        if optimize:
            # Keep dynamic constants out of the static HTML minifier.
            optimized = replace(optimized, body=optimize_nodes(optimized.body))

    comment = ''
    if template_comments:
        details = metadata if metadata is not None else {
            'name': template.source.filename,
            'source_sha256': sha256(template.source.text.encode('utf-8')).hexdigest(),
            'includes': [],
            'compiler_version': VERSION,
            'application_version': '',
        }
        comment = template_comment(details)
        end = len(template.source.text)
        optimized = replace(optimized, body=(*optimized.body, Text(comment, Span(template.source, end, end))))

    runtime_analysis = Analysis(optimized)
    runtime = _runtime(optimized, runtime_analysis, interface, minified=minify_html, comment=comment)
    code = runtime.finish()
    stub = _stub(template, interface)
    checking_code = checker.finish()
    for content in (code, stub, checking_code):
        compile(content, template.source.filename + '.py', 'exec')

    source_map: dict[str, object] = {
        'schema': 1,
        'offset_encoding': 'unicode-codepoints',
        'escaping_policy': ESCAPING_POLICY,
        'runtime': runtime.mappings,
        'checker': checker.mappings,
    }
    return Compilation(
        code,
        stub,
        checking_code,
        source_map,
        tuple(sorted(interface.context)),
        tuple(
            {'module': item.module, 'name': item.name, 'alias': item.alias}
            for item in runtime_analysis.all_imports()
        ),
    )

def compile_template(
    source: str, *, filename: str = '<template>', optimize: bool = True,
    minify_html: bool = False, template_comments: bool = False,
) -> Compilation:
    """Compile trusted templates, optionally baking in HTML minification and metadata.

    Static HTML is minified before metadata comments are added. Dynamic values keep
    their whitespace and HTML escaping. Helpers are never imported or executed.
    Includes and components need filesystem resolution; use Engine or build().
    """
    validate_compile_options(optimize=optimize, minify_html=minify_html, template_comments=template_comments)
    if not isinstance(source, str):
        raise TypeError('source must be a string')

    if not isinstance(filename, str):
        raise TypeError('filename must be a string')

    return compile_parsed(
        Parser(Source(source, filename)).parse(), optimize=optimize,
        minify_html=minify_html, template_comments=template_comments,
    )
