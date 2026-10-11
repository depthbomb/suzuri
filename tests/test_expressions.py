from html import escape
from types import coroutine
import pytest
from suzuri import TemplateRenderError, compile_template

@pytest.mark.parametrize('expression', [
    '[x for x in xs if y for y in ys]',
    '{x for x in xs if y for y in ys}',
    '{x: y for x in xs if y for y in ys}',
    'list(x for x in xs if y for y in ys)',
    '[(x, y) for x in xs for y in (y,)]',
    '[[y for _ in [0]] for x in xs if y for y in ys]',
])
@pytest.mark.parametrize('optimize', [False, True])
def test_comprehension_targets_are_local_throughout_their_scope(renderer, expression, optimize):
    context = {'xs': [1], 'ys': [2], 'y': True}
    with pytest.raises((UnboundLocalError, NameError)) as expected:
        eval(expression, dict(context))

    template = renderer(f'<out value="{expression}" />', optimize)
    for render in (template.render, lambda values: ''.join(template.iter_render(values))):
        with pytest.raises(TemplateRenderError) as actual:
            render(context)

        assert type(actual.value.__cause__) is type(expected.value)

@pytest.mark.parametrize('expression', [
    '[x for x in x]',
    '[(x, y) for x in x for y in x]',
    '[[x for x in x] for x in groups]',
    '[x for x in [y for y in x]]',
    '[[(x, y) for y in x] for x in groups]',
])
def test_nested_comprehensions_evaluate_first_iterable_in_surrounding_scope(renderer, expression):
    context = {'x': [[1, 2], [3]], 'groups': [[4, 5], [6]]}
    expected = escape(str(eval(expression, dict(context))), quote=True)
    template = renderer(f'<out value="{expression}" />')
    assert template.render(context) == expected
    assert ''.join(template.iter_render(context)) == expected

@pytest.mark.parametrize('expression', [
    "f'{name=}'",
    "f'{(name)=}'",
    "f'{name = }'",
    "f'prefix {name=!s:>10} suffix'",
    "f'{name=:>10}'",
    "f'{name=!a}'",
    "f'{name=}|{count=}'",
    "f'{f\"{name=}\"=}'",
    "f'{name # comment\n=}'",
    "f'{name = # comment\n}'",
    "f'{name:{width}}'",
    "f'{café=}'",
    "f'{name.replace(\"A\", \"B\")=}'",
    "f'{dict(value=name)=}'",
    "f'{[x for x in name]=}'",
    "f'{name == name=}'",
    "f'{{{name=}}}'",
])
@pytest.mark.parametrize('optimize', [False, True])
def test_formatted_strings_match_python_values_and_debug_labels(renderer, expression, optimize):
    context = {'name': 'Ada', 'count': 3, 'width': 8, 'café': '🐞'}
    source = '<out value="' + escape(expression, quote=True) + '" />'
    expected = escape(str(eval(expression, dict(context))), quote=True)
    template = renderer(source, optimize)
    assert template.render(context) == expected
    assert ''.join(template.iter_render(context)) == expected

def test_debug_strings_preserve_import_and_loop_binding_names(renderer):
    source = '<from module="math" import="pi" /><for each="item" in="items"><out value="f\'{item=} {pi=}\'" /></for>'
    assert renderer(source).render({'items': [2]}) == 'item=2 pi=3.141592653589793'

def test_debug_string_source_maps_cover_generated_characters():
    source = '<out value="f\'{café=}\'" />'
    compiled = compile_template(source)
    for output, entries in ((compiled.code, compiled.source_map['runtime']), (compiled.checker, compiled.source_map['checker'])):
        for entry in entries:
            if entry['kind'] == 'expression':
                expression = output[entry['generated_start']:entry['generated_end']]
                assert len(entry['source_offsets']) == len(expression) + 1
                assert all(entry['source']['start'] <= offset <= entry['source']['end'] for offset in entry['source_offsets'])

@pytest.mark.parametrize('base', [str, int, float])
def test_scalar_subclasses_keep_conversion_and_awaitable_checks(renderer, base):
    class CustomValue(base):
        def __str__(self):
            return '<custom>'

    class AwaitableValue(base):
        def __await__(self):
            yield

    template = renderer('<out value="value" />')
    assert template.render({'value': CustomValue('1')}) == '&lt;custom&gt;'
    with pytest.raises(TemplateRenderError, match='Awaitable'):
        template.render({'value': AwaitableValue('1')})

def test_generator_coroutines_are_rejected(renderer):
    @coroutine
    def value():
        yield

    awaitable = value()
    try:
        with pytest.raises(TemplateRenderError, match='Awaitable'):
            renderer('<out value="value" />').render({'value': awaitable})
    finally:
        awaitable.close()

@pytest.mark.parametrize('separator', ['\v', '\f', '\x85', '\u2028', '\u2029', '\r', '\r\n', '\n'])
@pytest.mark.parametrize('optimize', [False, True])
def test_python_physical_lines_preserve_unicode_names_and_offsets(renderer, separator, optimize):
    expression = "'''before" + separator + "after''' + café"
    source = '<out value="' + escape(expression, quote=True) + '" />'
    expected = escape(eval(expression, {'café': '🐞'}))
    module = renderer(source, optimize)
    assert module.render({'café': '🐞'}) == expected
    assert ''.join(module.iter_render({'café': '🐞'})) == expected
    compilation = compile_template(source, optimize=optimize)
    for mapping in compilation.source_map['runtime']:
        if mapping['kind'] == 'expression':
            generated = compilation.code[mapping['generated_start']:mapping['generated_end']]
            assert len(mapping['source_offsets']) == len(generated) + 1
            index = generated.index('_suzuri_context')
            assert mapping['source_offsets'][index] == source.index('café')
