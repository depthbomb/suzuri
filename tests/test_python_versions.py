import sys
import subprocess
from os import environ
from json import loads, dumps
from html import escape
from pathlib import Path
from types import ModuleType
from warnings import catch_warnings, simplefilter
from traceback import format_exception
import pytest
from suzuri import build, Engine, ArtifactError, compile_template, TemplateRenderError, TemplateSyntaxError

@pytest.mark.parametrize('name', ['sentinel', 'frozendict'])
@pytest.mark.parametrize('optimize', [False, True])
def test_new_builtin_names_remain_context_values(renderer, name, optimize):
    template = renderer(f'<out value="{name}" />', optimize)
    context = {name: '<context>'}
    assert template.render(context) == '&lt;context&gt;'
    assert ''.join(template.iter_render(context)) == '&lt;context&gt;'
    assert template.render_values(**context) == '&lt;context&gt;'
    with pytest.raises(TemplateRenderError) as error:
        template.render({})
    assert isinstance(error.value.__cause__, KeyError)

@pytest.mark.parametrize('name', ['sentinel', 'frozendict'])
def test_new_builtin_names_keep_include_and_component_bindings(tmp_path, name):
    source = tmp_path / 'templates'
    (source / 'components').mkdir(parents=True)
    (source / 'part.html').write_text(f'<out value="{name}" />', encoding='utf-8')
    (source / 'components' / 'value.html').write_text(f'<out value="{name}" />', encoding='utf-8')
    (source / 'page.html').write_text(
        f'<include src="part.html" /><include src="part.html" with="data" />'
        f'<component name="value" :{name}="value" />', encoding='utf-8',
    )
    engine = Engine(source)
    context = {name: 'parent', 'data': {name: 'child'}, 'value': 'component'}
    assert engine.render('page.html', context) == 'parentchildcomponent'

@pytest.mark.skipif(sys.version_info < (3, 15), reason='Python 3.15 built-ins')
@pytest.mark.parametrize('name,expression,expected', [
    ('frozendict', "frozendict(a=1)['a']", '1'),
    ('sentinel', "str(sentinel('missing'))", 'missing'),
])
def test_new_builtins_are_available_through_explicit_imports(renderer, name, expression, expected):
    template = renderer(f'<from module="builtins" import="{name}" /><out value="{expression}" />')
    assert template.render({name: 'ignored'}) == expected

@pytest.mark.parametrize('expression,context', [
    ('[*group for group in groups]', {'groups': [[1, 2], [3]]}),
    ('sorted({*group for group in groups})', {'groups': [[3, 1], [1, 2]]}),
    ('{**group for group in groups}', {'groups': [{'a': 1}, {'a': 2, 'b': '<b>'}]}),
    ('list((*group for group in groups))', {'groups': [[1, 2], [3]]}),
    ('[*group for group in groups if group]', {'groups': [[], [1, 2], []]}),
    ('{**group for row in rows for group in row}', {'rows': [[{'a': 1}], [{'b': 2}]]}),
    ('[[*group for group in groups] for groups in rows]', {'rows': [[[1], [2]], [[3]]]}),
    ('[*groups for groups in groups]', {'groups': [[1], [2]]}),
    ('{**groups for groups in groups}', {'groups': [{'a': 1}, {'b': 2}]}),
    ('{**café for café in données}', {'données': [{'clé': '<value>'}]}),
])
@pytest.mark.parametrize('optimize', [False, True])
def test_comprehension_unpacking_matches_the_running_python(renderer, expression, context, optimize):
    source = '<out value="' + escape(expression, quote=True) + '" />'
    if sys.version_info < (3, 15):
        with pytest.raises(TemplateSyntaxError, match='Invalid Python expression'):
            renderer(source, optimize)
        return

    expected = escape(str(eval(expression, dict(context))), quote=True)
    template = renderer(source, optimize)
    assert template.render(context) == expected
    assert ''.join(template.iter_render(context, chunk_size=1)) == expected
    assert template.render_values(**context) == expected

def test_compilation_records_its_python_target():
    expected = '>=3.15' if sys.version_info >= (3, 15) else '>=3.14'
    compilation = compile_template('hello')
    module = ModuleType('compiled_target')
    exec(compilation.code, module.__dict__)
    assert compilation.python_requires == module.__suzuri_python_requires__ == expected

@pytest.mark.skipif(sys.version_info < (3, 15), reason='Python 3.15 comprehension syntax')
def test_unpacking_checker_and_stub_support_python_315(tmp_path):
    compilation = compile_template(
        '<param name="groups" type="list[dict[str, int]]" />'
        '<out value="{**group for group in groups}" />',
    )
    checker = tmp_path / 'view_check.py'
    stub = tmp_path / 'view.pyi'
    checker.write_text(compilation.checker, encoding='utf-8')
    stub.write_text(compilation.stub, encoding='utf-8')
    result = subprocess.run(
        [sys.executable, '-m', 'mypy', '--no-incremental', '--python-version', '3.15', str(checker), str(stub)],
        capture_output=True, text=True, timeout=60,
    )
    assert result.returncode == 0, result.stdout + result.stderr

def test_compiler_imports_wait_until_source_compilation_on_315(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'page.html').write_text('hello', encoding='utf-8')
    bundle = build(source, tmp_path / 'bundle')
    run_python(sys.executable, '''from sys import argv, modules, version_info
from suzuri import Engine
engine = Engine(argv[1])
assert engine.render('page.html') == 'hello'
if version_info >= (3, 15):
    assert 'suzuri.compiler' not in modules
    assert 'suzuri.parser' not in modules
source = Engine(argv[2])
template = source.get_template('page.html')
template.iter_render().close()
if version_info >= (3, 15):
    assert 'suzuri.compiler' not in modules
assert template.render() == 'hello'
from suzuri import build, compile_template
assert callable(build)
assert compile_template('hello').context_names == ()
assert build(argv[2], argv[3]).template_names == ('page.html',)
''', bundle.path, source, tmp_path / 'another-bundle')

@pytest.mark.parametrize('target', [None, True, 315, [], {}, '>=3.13', '>=3.16', '>=3.14,<3.16'])
@pytest.mark.parametrize('verification', ['eager', 'on_load'])
def test_unknown_python_targets_are_rejected_before_loading(tmp_path, target, verification):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'page.html').write_text('hello', encoding='utf-8')
    bundle = build(source, tmp_path / 'bundle')
    manifest_path = bundle.path / 'manifest.json'
    manifest = loads(manifest_path.read_text(encoding='utf-8'))
    manifest['python_requires'] = target
    manifest_path.write_text(dumps(manifest), encoding='utf-8')
    with pytest.raises(ArtifactError, match='Python target'):
        Engine(bundle.path, verify_artifacts=verification)

def test_renderer_loader_retains_verified_source_for_tracebacks(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'page.html').write_text('<out value="value" />', encoding='utf-8')
    template = Engine(source).get_template('page.html')
    module = template.module
    path = Path(module.__file__)
    original = path.read_text(encoding='utf-8')
    path.unlink()
    assert module.__spec__.loader.get_source(module.__name__) == original
    with catch_warnings():
        simplefilter('error', DeprecationWarning)
        with pytest.raises(TemplateRenderError) as error:
            module.render({})
        assert "_suzuri_context['value']" in ''.join(format_exception(error.value))

@pytest.fixture
def python_versions():
    paths = {version: environ.get('SUZURI_PYTHON' + version.replace('.', '')) for version in ('3.14', '3.15')}
    if not all(paths.values()):
        pytest.skip('Set SUZURI_PYTHON314 and SUZURI_PYTHON315 to run cross-version checks')
    return paths

def run_python(interpreter, code, *arguments):
    result = subprocess.run([interpreter, '-c', code, *map(str, arguments)], capture_output=True, text=True, timeout=60)
    assert result.returncode == 0, result.stdout + result.stderr
    return result.stdout.strip()

def test_bundles_load_on_supported_runtimes_and_reject_older_ones(tmp_path, python_versions):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'page.html').write_text('<out value="sentinel" />', encoding='utf-8')
    for version, interpreter in python_versions.items():
        bundle = tmp_path / version
        run_python(interpreter, 'from sys import argv; from suzuri import build; build(argv[1], argv[2])', source, bundle)
        manifest = loads((bundle / 'manifest.json').read_text(encoding='utf-8'))
        assert manifest['python_requires'] == '>=' + version

    render = "from sys import argv; from suzuri import Engine; print(Engine(argv[1]).render('page.html', {'sentinel': 'context'}))"
    for version in python_versions:
        assert run_python(python_versions[version], render, tmp_path / '3.14') == 'context'
    assert run_python(python_versions['3.15'], render, tmp_path / '3.15') == 'context'
    for verification in ('eager', 'on_load'):
        run_python(python_versions['3.14'], '''from sys import argv
from suzuri import Engine, ArtifactError
try:
    Engine(argv[1], verify_artifacts=argv[2])
except ArtifactError as error:
    assert 'requires Python 3.15' in str(error)
else:
    raise AssertionError('Loaded a 3.15 bundle on 3.14')
''', tmp_path / '3.15', verification)

    manifest = loads((tmp_path / '3.15' / 'manifest.json').read_text(encoding='utf-8'))
    renderer = tmp_path / '3.15' / (manifest['templates']['page.html']['module'] + '.py')
    run_python(python_versions['3.14'], '''from sys import argv
from pathlib import Path
from suzuri import ArtifactError
try:
    exec(compile(Path(argv[1]).read_bytes(), argv[1], 'exec'), {})
except ArtifactError as error:
    assert 'requires Python 3.15' in str(error)
else:
    raise AssertionError('Executed a 3.15 renderer on 3.14')
''', renderer)

def test_source_cache_separates_python_targets(tmp_path, python_versions):
    source = tmp_path / 'templates'
    source.mkdir()
    page = source / 'page.html'
    page.write_text('built on 3.14', encoding='utf-8')
    code = "from sys import argv; from suzuri import Engine; print(Engine(argv[1], compiled_dir=argv[2]).render('page.html'))"
    cache = tmp_path / 'cache'
    assert run_python(python_versions['3.14'], code, source, cache) == 'built on 3.14'
    page.write_text('built on 3.15', encoding='utf-8')
    assert run_python(python_versions['3.15'], code, source, cache) == 'built on 3.15'
    page.unlink()
    assert run_python(python_versions['3.14'], code, source, cache) == 'built on 3.14'
    assert run_python(python_versions['3.15'], code, source, cache) == 'built on 3.15'
    assert len(list(cache.glob('*/manifest.json'))) == 2

def test_315_unpacking_bundle_is_rejected_before_parsing_on_314(tmp_path, python_versions):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'page.html').write_text('<out value="{**group for group in groups}" />', encoding='utf-8')
    bundle = tmp_path / 'bundle'
    run_python(python_versions['3.15'], 'from sys import argv; from suzuri import build; build(argv[1], argv[2])', source, bundle)
    result = run_python(python_versions['3.15'], "from sys import argv; from suzuri import Engine; print(Engine(argv[1]).render('page.html', {'groups': [{'a': 1}]}))", bundle)
    assert result == '{&#x27;a&#x27;: 1}'
    run_python(python_versions['3.14'], '''from sys import argv
from suzuri import Engine, ArtifactError
try:
    Engine(argv[1])
except ArtifactError as error:
    assert 'requires Python 3.15' in str(error)
else:
    raise AssertionError('Accepted Python 3.15 syntax on 3.14')
''', bundle)
