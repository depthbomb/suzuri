from json import loads
from hashlib import sha256
from types import ModuleType
import pytest
from suzuri import build, Engine, __version__, compile_template, TemplateRenderError

def compile_module(source, **options):
    compilation = compile_template(source, filename='page.html', **options)
    module = ModuleType('compiled_options')
    exec(compile(compilation.code, 'page.html.py', 'exec'), module.__dict__)
    return compilation, module

def make_bundle(tmp_path, source, **options):
    templates = tmp_path / 'templates'
    templates.mkdir()
    (templates / 'page.html').write_bytes(source.encode('utf-8'))
    return build(templates, tmp_path / 'views', **options)

@pytest.mark.parametrize('optimize', [False, True])
@pytest.mark.parametrize('source,context,expected', [
    ('\n<p  class="card"> Hello   world </p>\n', {}, '<p class=card>Hello world</p>'),
    ('<p>\n Hello <out value="name" /> !\n</p>', {'name': 'Ada'}, '<p>Hello Ada !</p>'),
    ('<p>\n<out value="name" />\n</p>', {'name': ' a\n  b '}, '<p> a\n  b </p>'),
    ('<p>A<if test="flag"> </if>B</p>', {'flag': True}, '<p>A B</p>'),
    ('<p>A<if test="flag"> </if>B</p>', {'flag': False}, '<p>AB</p>'),
    ('<p>A <if test="flag"><b>x</b></if> B</p>', {'flag': False}, '<p>A  B</p>'),
    ('<p>A <if test="flag"><div>x</div></if> B</p>', {'flag': False}, '<p>A  B</p>'),
    ('<ul>\n<for each="item" in="items">\n<if test="item"><li>x</li></if><else><li>y</li></else>\n</for>\n</ul>', {'items': [True, False]}, '<ul><li>x</li><li>y</li></ul>'),
    ('<ul>\n<for each="item" in="items">\n<li> <out value="item" /> </li>\n</for>\n</ul>', {'items': ['a', 'b']}, '<ul><li>a</li><li>b</li></ul>'),
    ('<ul>\n<for each="item" in="items">\n<li> <out value="item" /> </li>\n</for>\n</ul>', {'items': []}, '<ul></ul>'),
    ('<p>A<for each="item" in="items"> <out value="item" /></for>B</p>', {'items': [1, 2]}, '<p>A 1 2B</p>'),
    ('<pre>\n  before <out value="value" />\n  after</pre>', {'value': ' a\n b '}, '<pre>\n  before  a\n b \n  after</pre>'),
    ('<!-- remove --><script>\n // keep\n const x = " a  b ";\n</script><style>\n p { content: "a  b"; }\n</style>', {}, '<script>\n // keep\n const x = " a  b ";\n</script><style>\n p { content: "a  b"; }\n</style>'),
    ('<!--!suzuri-compile:0--><p> a <out value="value" /> b </p>', {'value': 'x'}, '<!--!suzuri-compile:0--><p>a x b</p>'),
    ('&am<!-- remove --><out value="name" />', {'name': 'p;'}, '&am<!---->p;'),
    ('&<out value="name" /><!-- remove -->p;', {'name': 'am'}, '&am<!---->p;'),
    ('&<out value="name" />m<!-- remove -->p;', {'name': 'a'}, '&am<!---->p;'),
    ('&am<!-- remove --><if test="flag">p;</if>', {'flag': True}, '&am<!---->p;'),
    ('<<!-- remove --><out value="name" />', {'name': 'b>'}, '<<!---->b&gt;'),
])
def test_minification_is_baked_into_both_generated_renderers(source, context, expected, optimize):
    compiled, module = compile_module(source, minify_html=True, optimize=optimize)
    assert module.render(context) == expected
    assert ''.join(module.iter_render(context, chunk_size=1)) == expected
    assert module.__suzuri_minified__ is True
    assert 'minify_html(' not in compiled.code
    assert '<!--!suzuri-compile::' not in module.render(context)

@pytest.mark.parametrize('minify', [False, True])
def test_compiled_metadata_is_literal_and_follows_html_processing(minify):
    source = '<!-- ordinary --><p  > Hello <out value="name" /> </p>'
    compiled, module = compile_module(source, minify_html=minify, template_comments=True)
    result = module.render_values(name='<Ada>')
    assert '<!-- suzuri ' in compiled.code
    assert result.endswith(module.__suzuri_template_comment__)
    metadata = loads(module.__suzuri_template_comment__[len('<!-- suzuri '):-4])
    assert metadata == {
        'name': 'page.html',
        'source_sha256': sha256(source.encode()).hexdigest(),
        'includes': [],
        'compiler_version': __version__,
        'application_version': '',
    }
    assert result.count('<!-- suzuri ') == 1
    assert ('<!-- ordinary -->' in result) is not minify
    assert '&lt;Ada&gt;' in result
    assert ''.join(module.iter_render({'name': '<Ada>'})) == result

@pytest.mark.parametrize('value', [' a  b ', '\n\t a\n b \t', ' ', '', '<tag>&', '\r\n'])
@pytest.mark.parametrize('wrapper', ['{}', '<p>{}</p>', '<if test="True">{}</if>', '<for each="i" in="range(2)">{}</for>'])
def test_constant_outputs_keep_their_whitespace_during_minification(value, wrapper):
    from html import escape

    source = wrapper.format('<out value="' + escape(repr(value), quote=True) + '" />')
    _, reference = compile_module(source, optimize=False, minify_html=True)
    _, optimized = compile_module(source, optimize=True, minify_html=True)
    expected = reference.render({})
    assert optimized.render({}) == expected
    assert ''.join(optimized.iter_render({}, chunk_size=1)) == expected

@pytest.mark.parametrize('minify', [False, True])
@pytest.mark.parametrize('comments', [False, True])
def test_build_options_persist_and_do_not_need_engine_processing(tmp_path, minify, comments, monkeypatch):
    source = '<!-- ordinary -->\n<p  class="card"> Hello <out value="name" /> </p>\n'
    bundle = make_bundle(tmp_path, source, minify_html=minify, template_comments=comments, application_version='app-2')
    assert bundle.manifest['options']['minify_html'] is minify
    assert bundle.manifest['options']['template_comments'] is comments
    module_name = bundle.manifest['templates']['page.html']['module']
    code = (bundle.path / (module_name + '.py')).read_text(encoding='utf-8')
    if minify:
        assert '<p class=card>Hello ' in code and '<!-- ordinary -->' not in code

    if comments:
        assert '<!-- suzuri ' in code

    monkeypatch.setattr('suzuri.rendering.minify_html', lambda *args: pytest.fail('Runtime minification was not requested'))
    monkeypatch.setattr('suzuri.loader.template_comment', lambda *args: pytest.fail('Metadata was already compiled'))
    template = Engine(bundle.path).get_template('page.html')
    result = template.render({'name': '<Ada>'})
    assert ('<!-- suzuri ' in result) is comments
    if comments:
        assert loads(result.rsplit('<!-- suzuri ', 1)[1][:-4])['application_version'] == 'app-2'

    assert ''.join(template.iter_render({'name': '<Ada>'}, chunk_size=1)) == result
    assert template.module.render_values(name='<Ada>') == result
    if minify and comments:
        assert Engine(bundle.path, minify_html=True, template_comments=True).render('page.html', {'name': '<Ada>'}) == result

def test_runtime_minification_preserves_compiled_comment_without_duplicating_it(tmp_path):
    bundle = make_bundle(tmp_path, '<p  > a  b </p>', template_comments=True)
    engine = Engine(bundle.path, minify_html=True, template_comments=True)
    output = engine.render('page.html')
    assert output.startswith('<p>a b</p><!-- suzuri ')
    assert output.count('<!-- suzuri ') == 1
    assert ''.join(engine.iter_render('page.html', chunk_size=1)) == output

def test_compiled_minification_keeps_streaming_lazy_with_instance_options(tmp_path):
    bundle = make_bundle(
        tmp_path, '<for each="item" in="items"><out value="record(item)" /></for>',
        minify_html=True, template_comments=True,
    )
    events = []
    calls = []
    engine = Engine(bundle.path, minify_html=True, template_comments=True, on_render=events.append, chunk_size=1)
    stream = engine.iter_render('page.html', {'items': [1, 2, 3], 'record': lambda item: calls.append(item) or item})
    assert calls == []
    assert next(stream) == '1'
    stream.close()
    assert calls == [1]
    assert len(events) == 1 and events[0].status == 'closed' and events[0].output_chars == 1

def test_includes_are_minified_in_parent_context_with_original_error_locations(tmp_path):
    templates = tmp_path / 'templates'
    templates.mkdir()
    parent = '<main>\n<include src="part.html" />\n</main><pre><include src="part.html" /></pre>'
    part = '<p>\n  Hello <out value="name" /> \n</p>'
    (templates / 'page.html').write_bytes(parent.encode('utf-8'))
    (templates / 'part.html').write_bytes(part.encode('utf-8'))
    bundle = build(templates, tmp_path / 'views', minify_html=True, template_comments=True)
    template = Engine(bundle.path).get_template('page.html')
    output = template.render({'name': 'Ada'})
    assert output.startswith('<main><p>Hello Ada</p></main><pre><p>\n  Hello Ada \n</p></pre>')
    metadata = loads(template.module.__suzuri_template_comment__[len('<!-- suzuri '):-4])
    assert metadata['includes'] == ['part.html']
    assert metadata['source_sha256'] == sha256((templates / 'page.html').read_bytes()).hexdigest()
    assert output.count('<!-- suzuri ') == 1
    with pytest.raises(TemplateRenderError) as error:
        template.render()

    assert (error.value.filename, error.value.line, error.value.column) == ('part.html', 2, 9)

def test_minification_retains_expression_source_offsets():
    source = '<p>\n  Hello <out value="user.name" />\n</p>'
    compiled, module = compile_module(source, minify_html=True, template_comments=True)
    for mapping in compiled.source_map['runtime']:
        if mapping['kind'] == 'expression':
            assert mapping['source']['line'] == 2
            assert all(0 <= offset <= len(source) for offset in mapping['source_offsets'])

    with pytest.raises(TemplateRenderError) as error:
        module.render({})

    assert (error.value.line, error.value.column) == (2, 9)

@pytest.mark.parametrize('option', ['minify_html', 'template_comments'])
@pytest.mark.parametrize('value', [None, 1, 'yes'])
def test_build_and_compile_validate_output_options_before_creating_files(tmp_path, option, value):
    with pytest.raises(TypeError, match=f'{option} must be a boolean'):
        compile_template('hello', **{option: value})

    with pytest.raises(TypeError, match=f'{option} must be a boolean'):
        make_bundle(tmp_path, 'hello', **{option: value})

    assert not (tmp_path / 'views').exists()

def test_first_render_bakes_options_into_cached_module(tmp_path):
    templates = tmp_path / 'templates'
    templates.mkdir()
    (templates / 'page.html').write_text('<p  class="card"> hello  world </p>', encoding='utf-8')
    output = tmp_path / 'views'
    engine = Engine(templates, compiled_dir=output, minify_html=True, template_comments=True)
    assert not output.exists()
    rendered = engine.render('page.html')
    assert rendered.startswith('<p class=card>hello world</p><!-- suzuri ')
    assert rendered.count('<!-- suzuri ') == 1
    code = next(path for path in output.rglob('t_*.py') if not path.name.endswith('_check.py')).read_text(encoding='utf-8')
    assert '<p class=card>hello world</p>' in code and '<!-- suzuri ' in code

def test_compiled_options_are_deterministic_and_keep_the_original_interface(tmp_path):
    source = '<param name="value" type="str" /><p> <if test="False"><out value="value" /></if> stable </p>'
    first = make_bundle(tmp_path, source, minify_html=True, template_comments=True)
    second = build(tmp_path / 'templates', tmp_path / 'again', minify_html=True, template_comments=True)
    assert first.manifest == second.manifest
    assert {path.name: path.read_bytes() for path in first.path.iterdir()} == {
        path.name: path.read_bytes() for path in second.path.iterdir()
    }
    module = first.manifest['templates']['page.html']['module']
    stub = (first.path / (module + '.pyi')).read_text(encoding='utf-8')
    assert 'value: (str)' in stub
    assert Engine(first.path).render('page.html').startswith('<p>stable</p><!-- suzuri ')

def test_nested_control_flow_keeps_evaluation_order_and_escaping():
    source = '''<ul>
<for each="row" in="rows">
<if test="row"><for each="value" in="row"><li> <out value="record(value)" /> </li></for></if>
<else><li> empty </li></else>
</for>
</ul>'''
    _, module = compile_module(source, minify_html=True, template_comments=True)
    calls = []
    context = {'rows': [['<a>', 'b'], [], ['c']], 'record': lambda value: calls.append(value) or value}
    result = module.render(context)
    # The first branch can be empty at runtime, so surrounding spaces stay conservative.
    assert result.startswith('<ul> <li>&lt;a&gt;</li><li>b</li>  <li>empty</li>  <li>c</li> </ul><!-- suzuri ')
    assert calls == ['<a>', 'b', 'c']
    calls.clear()
    assert ''.join(module.iter_render(context, chunk_size=1)) == result
    assert calls == ['<a>', 'b', 'c']
