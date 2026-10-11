import sys
import asyncio
import subprocess
from json import loads
from pathlib import Path
from shutil import rmtree
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
import pytest
from suzuri import build, Engine, BuildError, TemplateRenderError, TemplateSyntaxError

def write(root, name, text):
    path = root / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.encode('utf-8'))
    return path

def view(tmp_path, parent, files, **options):
    root = tmp_path / 'templates'
    write(root, 'page.html', parent)
    for name, text in files.items():
        write(root, name, text)
    engine = Engine(root, compiled_dir=tmp_path / 'views', **options)
    return engine.get_template('page.html')

@pytest.mark.parametrize('minify', [False, True])
def test_include_values_override_only_the_partial_and_inherit_context(tmp_path, minify):
    template = view(tmp_path,
        '<out value="user" />|<include src="card.html" with="{\'user\': author}" />|<out value="user" />',
        {'card.html': '<param name="user" type="str" /><out value="user" />:<out value="suffix" />'}, minify_html=minify)
    context = {'user': 'parent', 'author': '<Ada>', 'suffix': '!'}
    expected = 'parent|&lt;Ada&gt;:!|parent'
    assert template.render(context) == expected
    assert ''.join(template.iter_render(context, chunk_size=1)) == expected
    assert context == {'user': 'parent', 'author': '<Ada>', 'suffix': '!'}
    assert template.module.render_values(**context) == expected

def test_partial_input_names_are_not_required_from_caller_when_supplied(tmp_path):
    template = view(tmp_path, '<include src="card.html" with="{\'user\': author}" />', {
        'card.html': '<param name="user" type="str" /><out value="user" />',
    })
    assert template.module.render_values(author='Ada') == 'Ada'
    manifest = loads((Path(template.module.__file__).parent / 'manifest.json').read_text())
    assert manifest['templates']['page.html']['context_names'] == ['author']

def test_dynamic_mapping_supports_optional_inherited_values_and_render_values(tmp_path):
    template = view(tmp_path, '<include src="card.html" with="data" />', {
        'card.html': '<param name="user" type="str" /><out value="user" />:<out value="suffix" />',
    })
    assert template.module.render_values(data={'user': 'Ada', 'suffix': '!'}) == 'Ada:!'
    assert template.module.render_values(data=UserDict({'user': 'local'}), user='parent', suffix='?') == 'local:?'
    assert template.render({'data': {}, 'user': 'parent', 'suffix': '!'}) == 'parent:!'
    with pytest.raises(TemplateRenderError) as error:
        template.render({'data': {}})
    assert error.value.filename == 'card.html'
    stub = Path(template.module.__file__).with_suffix('.pyi').read_text()
    assert '_suzuri_NotRequired' in stub

def test_dynamic_include_does_not_make_parent_reads_optional(tmp_path):
    template = view(tmp_path, '<include src="card.html" with="data" /><out value="user" />', {'card.html': '<out value="user" />'})
    with pytest.raises(TypeError, match='user'):
        template.module.render_values(data={'user': 'Ada'})
    assert template.module.render_values(data={'user': 'local'}, user='parent') == 'localparent'

def test_loop_scope_and_nested_overrides_are_restored(tmp_path):
    template = view(tmp_path,
        '<for each="item" in="items"><include src="first.html" with="{\'item\': item.upper()}" /><out value="item" /></for>', {
        'first.html': '<out value="item" /><include src="second.html" with="{\'item\': item + \'!\'}" /><out value="item" />',
        'second.html': '<out value="item" /><include src="third.html" />',
        'third.html': '<out value="item" />',
    })
    assert template.module.render_values(items=['a', 'b']) == 'AA!A!AaBB!B!Bb'

def test_data_expressions_run_once_in_order_and_stream_lazily(tmp_path):
    calls = []
    template = view(tmp_path,
        '<for each="item" in="items"><include src="part.html" with="record(item)" /></for>',
        {'part.html': '<out value="value" />'}, minify_html=True)

    def record(item):
        calls.append(item)
        return {'value': item}

    stream = template.iter_render({'items': range(4), 'record': record}, chunk_size=1)
    assert calls == []
    assert next(stream) == '0'
    assert calls == [0]
    stream.close()
    assert calls == [0]

@pytest.mark.parametrize('data', [None, [], [('user', 'Ada')], {1: 'Ada'}, 'text'])
def test_include_requires_a_mapping_with_string_keys(tmp_path, data):
    template = view(tmp_path, '\n<include src="part.html" with="data" />', {'part.html': 'static'})
    with pytest.raises(TemplateRenderError, match='mapping with string keys') as error:
        template.render({'data': data})
    assert (error.value.filename, error.value.line) == ('page.html', 2)

def test_async_mapping_helpers_are_rejected_and_closed(tmp_path):
    template = view(tmp_path, '<include src="part.html" with="data" />', {'part.html': 'static'})
    async def helper():
        return {}
    data = helper()
    with pytest.raises(TemplateRenderError, match='Awaitable'):
        template.render({'data': data})
    assert data.cr_frame is None

@pytest.mark.parametrize('minify', [False, True])
def test_component_attributes_are_strings_or_bound_expressions(tmp_path, minify):
    template = view(tmp_path,
        '<component name="alert" type="danger" icon="warning" label="False" :active="False" :message="message" />', {
        'components/alert.html': '<param name="active" type="bool" /><p><out value="type" />:<out value="icon" />:<out value="label" />:<out value="active" />:<out value="message" /></p>',
    }, minify_html=minify, template_comments=True)
    output = template.render({'message': '<Oops>'})
    assert output.startswith('<p>danger:warning:False:False:&lt;Oops&gt;</p><!-- suzuri ')
    assert ''.join(template.iter_render({'message': '<Oops>'}, chunk_size=1)) == output
    assert output.count('<!-- suzuri ') == 1
    metadata = loads(output.split('<!-- suzuri ', 1)[1][:-4])
    assert metadata['includes'] == ['components/alert.html']

def test_components_have_isolated_inputs(tmp_path):
    template = view(tmp_path, '<component name="alert" />', {'components/alert.html': '<out value="secret" />'})
    with pytest.raises(TemplateSyntaxError, match='Missing component inputs') as error:
        template.render({'secret': 'parent context'})
    assert error.value.filename == 'page.html'

def test_component_props_do_not_leak_back_to_parent(tmp_path):
    template = view(tmp_path, '<component name="alert" message="child" /><out value="message" />', {
        'components/alert.html': '<out value="message" />',
    })
    assert template.render({'message': 'parent'}) == 'childparent'

def test_nested_component_names_resolve_from_source_root(tmp_path):
    template = view(tmp_path, '<include src="pages/part.html" with="{}" />', {
        'pages/part.html': '<component name="forms/field" :value="value" />',
        'components/forms/field.html': '<include src="../label.html" with="{\'label\': value}" />',
        'components/label.html': '<out value="label" />',
    })
    assert template.render({'value': 'nested'}) == 'nested'

def test_sibling_scopes_keep_imports_and_types_separate(tmp_path):
    template = view(tmp_path,
        '<component name="one" :value="4" /><component name="two" :value="\'ada lovelace\'" />', {
        'components/one.html': '<from module="math" import="sqrt" as="convert" /><param name="value" type="int" /><out value="convert(value)" />',
        'components/two.html': '<from module="string" import="capwords" as="convert" /><param name="value" type="str" /><out value="convert(value)" />',
    })
    assert template.render() == '2.0Ada Lovelace'
    checker = Path(template.module.__file__).with_name(Path(template.module.__file__).stem + '_check.py')
    result = subprocess.run([sys.executable, '-m', 'mypy', '--no-incremental', str(checker)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

def test_include_with_data_inherits_parent_helpers(tmp_path):
    template = view(tmp_path, '<from module="string" import="capwords" /><include src="part.html" with="{\'name\': name}" />', {
        'part.html': '<out value="capwords(name)" />',
    })
    assert template.module.render_values(name='ada lovelace') == 'Ada Lovelace'

def test_component_prop_evaluation_order_and_unused_props(tmp_path):
    calls = []
    template = view(tmp_path, '<component name="alert" :first="record(1)" :second="record(2)" />', {'components/alert.html': 'static'})
    assert template.render({'record': lambda value: calls.append(value)}) == 'static'
    assert calls == [1, 2]

@pytest.mark.parametrize('tag,message', [
    ('<component />', 'missing'),
    ('<component name="alert" :name="name" />', 'reserved'),
    ('<component name="alert" type="a" :type="b" />', 'Duplicate'),
    ('<component name="alert" data-id="x" />', 'Invalid'),
    ('<component name="alert" class="x" />', 'Invalid'),
    ('<component name="alert" _suzuri_x="x" />', 'reserved'),
    ('<component name="alert" :value="" />', 'Expected a Python expression'),
    ('<component name="alert"></component>', 'self-closing'),
    ('<component name="../alert" />', 'Component names'),
    ('<component name="/alert" />', 'Component names'),
    ('<component name="forms\\alert" />', 'Component names'),
    ('<component name="" />', 'Component names'),
])
def test_invalid_component_syntax_is_reported_at_callsite(tmp_path, tag, message):
    template = view(tmp_path, tag, {'components/alert.html': 'x'})
    with pytest.raises(TemplateSyntaxError, match=message) as error:
        template.render()
    assert error.value.filename == 'page.html'

def test_component_cycles_are_rejected(tmp_path):
    template = view(tmp_path, '<component name="alert" />', {'components/alert.html': '<component name="alert" />'})
    with pytest.raises(BuildError, match='cycle'):
        template.render()

def test_component_error_locations_point_to_expression_or_component(tmp_path):
    template = view(tmp_path, '<component name="alert" :value="missing" />', {'components/alert.html': '<out value="value[0]" />'})
    with pytest.raises(TemplateRenderError) as caller:
        template.render()
    assert caller.value.filename == 'page.html'
    with pytest.raises(TemplateRenderError) as child:
        template.render({'missing': []})
    assert child.value.filename == 'components/alert.html'

def test_components_and_partials_keep_raw_content_during_minification(tmp_path):
    raw = '<script>\n  const x = " a  b ";\n</script><style>\n  p { color: red; }\n</style>'
    template = view(tmp_path, '<main>\n<component name="alert" message="hello" />\n</main><pre><include src="part.html" with="{}" /></pre>', {
        'components/alert.html': raw + '<p> <out value="message" /> </p>',
        'part.html': '\n  keep  this\n',
    }, minify_html=True)
    assert template.render() == '<main>' + raw + '<p>hello</p></main><pre>\n  keep  this\n</pre>'

def test_async_and_concurrent_renders_keep_component_data_local(tmp_path):
    template = view(tmp_path, '<component name="alert" :message="message" />', {'components/alert.html': '<out value="message" />'})
    async def run():
        assert await template.render_async({'message': '<Ada>'}) == '&lt;Ada&gt;'
        assert ''.join([chunk async for chunk in template.iter_render_async({'message': 'stream'})]) == 'stream'
    asyncio.run(run())
    with ThreadPoolExecutor(max_workers=8) as workers:
        results = list(workers.map(lambda value: template.render({'message': value}), range(30)))
    assert results == list(map(str, range(30)))

def test_component_edits_wait_for_parent_cache_deletion(tmp_path):
    template = view(tmp_path, '<component name="alert" />', {'components/alert.html': 'old'})
    assert template.render() == 'old'
    write(tmp_path / 'templates', 'components/alert.html', 'new')
    assert template.render() == 'old'
    rmtree(Path(template.module.__file__).parent)
    assert template.render() == 'new'

@pytest.mark.parametrize('kind', ['partial', 'component'])
def test_typed_inputs_report_incorrect_values(tmp_path, kind):
    child = '<param name="count" type="int" /><out value="count + 1" />'
    parent = '<include src="part.html" with="{\'count\': \'wrong\'}" />' if kind == 'partial' else '<component name="counter" count="wrong" />'
    files = {'part.html': child} if kind == 'partial' else {'components/counter.html': child}
    root = tmp_path / 'templates'
    write(root, 'page.html', parent)
    for name, text in files.items():
        write(root, name, text)
    result = build(root, tmp_path / 'views')
    module = result.manifest['templates']['page.html']['module']
    checker = result.path / (module + '_check.py')
    checked = subprocess.run([sys.executable, '-m', 'mypy', '--no-incremental', str(checker)], capture_output=True, text=True)
    assert checked.returncode == 1 and 'Incompatible types in assignment' in checked.stdout, checked.stdout + checked.stderr

def test_include_bindings_can_override_builtin_names_and_keep_builtin_fallbacks(tmp_path):
    template = view(tmp_path,
        '<for each="type" in="types"><include src="part.html" with="data" /></for>',
        {'part.html': '<out value="type" />:<out value="len(items)" />;'},
    )
    assert template.render({'types': ['loop'], 'data': {'type': 'local'}, 'items': [1, 2]}) == 'local:2;'
    assert template.render({'types': ['loop'], 'data': {'len': lambda value: 99}, 'items': [1, 2]}) == 'loop:99;'

def test_static_mapping_handles_multiline_expressions_entities_and_debug_fstrings(tmp_path):
    parent = '''<include src="part.html" with="{
        'value': f'{café=}' if café &lt; limit
        else 'large'
    }" />'''
    template = view(tmp_path, parent, {'part.html': '<out value="value" />'})
    assert template.render({'café': 2, 'limit': 3}) == 'café=2'
    assert template.render({'café': 4, 'limit': 3}) == 'large'
    path = Path(template.module.__file__)
    source_map = loads(path.with_suffix('.map.json').read_text(encoding='utf-8'))
    checker = path.with_name(path.stem + '_check.py').read_text(encoding='utf-8')
    expressions = [mapping for mapping in source_map['checker'] if mapping['kind'] == 'expression']
    assert any('café' in checker[item['generated_start']:item['generated_end']] for item in expressions)
    for item in expressions:
        assert len(item['source_offsets']) == item['generated_end'] - item['generated_start'] + 1

def test_typed_dynamic_mapping_callers_can_omit_overridden_inputs(tmp_path, monkeypatch):
    template = view(tmp_path, '<include src="part.html" with="data" />', {
        'part.html': '<param name="value" type="str" /><out value="value" />',
    })
    module = template.module
    path = Path(module.__file__)
    caller = write(path.parent, 'caller.py', f'from {path.stem} import render_values, render\nrender_values(data={{"value": "Ada"}})\nrender({{"data": {{"value": "Ada"}}}})\n')
    checker = path.with_name(path.stem + '_check.py')
    monkeypatch.setenv('MYPYPATH', str(path.parent))
    result = subprocess.run([sys.executable, '-m', 'mypy', '--no-incremental', str(caller), str(checker)], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

def test_scoped_annotation_imports_are_preserved(tmp_path):
    template = view(tmp_path,
        '<from module="decimal" import="Decimal" /><component name="number" :value="Decimal(2)" />', {
        'components/number.html': '<from module="decimal" import="Decimal" as="Value" /><param name="value" type="Value" /><out value="value + Value(1)" />',
    })
    assert template.render() == '3'
    path = Path(template.module.__file__)
    result = subprocess.run([sys.executable, '-m', 'mypy', '--no-incremental', str(path.with_name(path.stem + '_check.py'))], capture_output=True, text=True)
    assert result.returncode == 0, result.stdout + result.stderr

def test_component_rejects_awaitable_prop_even_when_unused(tmp_path):
    template = view(tmp_path, '<component name="alert" :unused="value" />', {'components/alert.html': 'static'})
    async def helper():
        return 1
    value = helper()
    with pytest.raises(TemplateRenderError, match='Awaitable'):
        template.render({'value': value})
    assert value.cr_frame is None

@pytest.mark.parametrize('optimize', [False, True])
def test_scoped_rendering_preserves_evaluation_when_optimizing(tmp_path, optimize):
    root = tmp_path / 'templates'
    write(root, 'page.html', '<component name="counter" :value="record(2)" />')
    write(root, 'components/counter.html', '<param name="value" type="int" /><if test="True"><include src="../part.html" with="{\'value\': value}" /></if><else>unused</else>')
    write(root, 'part.html', '<p> <out value="value" /> </p>')
    result = build(root, tmp_path / 'views', pattern='page.html', optimize=optimize, minify_html=True)
    calls = []

    def record(value):
        calls.append(value)
        return value

    template = Engine(result.path).get_template('page.html')
    assert template.render({'record': record}) == '<p>2</p>'
    assert calls == [2]
    assert ''.join(template.iter_render({'record': record})) == '<p>2</p>'
    assert calls == [2, 2]
