import asyncio
import subprocess
from sys import executable
from json import dumps
from pathlib import Path
from hashlib import sha256
from collections import UserDict
from concurrent.futures import ThreadPoolExecutor
import pytest
from suzuri.runtime import VERSION, ABI_VERSION, PYTHON_REQUIRES, ESCAPING_POLICY, COMPILER_REVISION
from suzuri import build, Engine, Template, SuzuriError, TemplateNotFound, compile_template, TemplateRenderError, TemplateSyntaxError

@pytest.fixture(params=['source', 'bundle'])
def engine_factory(tmp_path, request):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'page.html').write_text('<out value="value" />', encoding='utf-8')
    (source / 'static.html').write_text('static', encoding='utf-8')
    if request.param == 'bundle':
        bundle = build(source, tmp_path / 'bundle')
        return lambda **options: Engine(bundle.path, verify_artifacts='on_load', **options)

    return lambda **options: Engine(source, compiled_dir=tmp_path / 'cache', **options)

def test_template_handles_are_consistent_and_loading_is_lazy(engine_factory, monkeypatch):
    engine = engine_factory()
    events = []
    view = engine.get_template('page.html')
    assert type(view) is Template
    assert view.name == 'page.html'
    assert view is engine.get_template('page.html')
    assert view != engine_factory().get_template('page.html')
    assert len({view, engine_factory().get_template('page.html')}) == 2
    original_exec = exec

    def tracked_exec(*args, **kwargs):
        events.append('load')
        return original_exec(*args, **kwargs)

    monkeypatch.setattr('builtins.exec', tracked_exec)
    view.iter_render({'value': 'unused'}).close()
    asyncio.run(view.iter_render_async({'value': 'unused'}).aclose())
    assert events == []
    assert view.render({'value': '<Ada>'}) == '&lt;Ada&gt;'
    assert events == ['load']
    assert view.render({'value': 'again'}) == 'again'
    assert events == ['load']

def test_concurrent_module_access_loads_one_renderer(engine_factory):
    view = engine_factory().get_template('page.html')
    with ThreadPoolExecutor(max_workers=8) as workers:
        modules = list(workers.map(lambda _: view.module, range(30)))

    assert all(module is modules[0] for module in modules)

@pytest.mark.parametrize('defaults', [{}, {'value': 'default'}])
@pytest.mark.parametrize('target', ['engine', 'template'])
@pytest.mark.parametrize('asynchronous', [False, True])
def test_streams_take_shallow_context_snapshots(engine_factory, defaults, target, asynchronous):
    engine = engine_factory(default_context=defaults)
    context = UserDict(value=['original'])
    renderer = engine if target == 'engine' else engine.get_template('page.html')
    args = ('page.html', context) if target == 'engine' else (context,)
    stream = renderer.iter_render_async(*args) if asynchronous else renderer.iter_render(*args)
    shared = context['value']
    context['value'] = 'replaced'
    shared.append('shared')

    async def consume():
        return ''.join([chunk async for chunk in stream])

    output = asyncio.run(consume()) if asynchronous else ''.join(stream)
    assert output == '[&#x27;original&#x27;, &#x27;shared&#x27;]'
    assert dict(engine.default_context) == defaults

@pytest.mark.parametrize('method', ['render', 'iter_render', 'render_async', 'iter_render_async'])
@pytest.mark.parametrize('context', [[], [('value', 'wrong')], 'wrong', 42, {1: 'wrong'}])
@pytest.mark.parametrize('target', ['engine', 'template'])
def test_invalid_context_never_loads_or_compiles_a_renderer(engine_factory, method, context, target, monkeypatch):
    engine = engine_factory()
    renderer = engine if target == 'engine' else engine.get_template('static.html')
    args = ('static.html', context) if target == 'engine' else (context,)
    monkeypatch.setattr(engine, '_get_compiled', lambda *args: pytest.fail('Invalid arguments loaded a renderer'))
    monkeypatch.setattr('builtins.exec', lambda *args: pytest.fail('Invalid arguments executed a renderer'))
    with pytest.raises(TypeError, match='context must be a mapping with string keys or None'):
        result = getattr(renderer, method)(*args)
        if method == 'render_async':
            asyncio.run(result)

@pytest.mark.parametrize('method', ['iter_render', 'iter_render_async'])
@pytest.mark.parametrize('size,exception', [(True, TypeError), ('1', TypeError), (1.5, TypeError), (0, ValueError), (-1, ValueError)])
def test_bad_stream_options_fail_at_creation(engine_factory, method, size, exception, monkeypatch):
    engine = engine_factory()
    monkeypatch.setattr(engine, '_get_compiled', lambda *args: pytest.fail('Invalid arguments loaded a renderer'))
    with pytest.raises(exception, match='chunk_size must be a positive integer'):
        getattr(engine, method)('static.html', chunk_size=size)

@pytest.mark.parametrize('name,exception', [
    ([], TypeError), ({}, TypeError), (None, TypeError), (Path('page.html'), TypeError),
    ('', ValueError), ('../page.html', ValueError), ('./page.html', ValueError),
    ('/page.html', ValueError), ('C:/page.html', ValueError), ('a\\page.html', ValueError),
    ('a//page.html', ValueError), ('bad\0name', ValueError),
])
def test_name_validation_is_consistent(engine_factory, name, exception):
    with pytest.raises(exception):
        engine_factory().get_template(name)

def test_missing_template_has_a_dedicated_error(engine_factory):
    engine = engine_factory()
    with pytest.raises(TemplateNotFound) as caught:
        engine.get_template('missing.html')

    assert isinstance(caught.value, (SuzuriError, LookupError))
    assert caught.value.name == 'missing.html'
    assert caught.value.directory == engine.path
    assert 'missing.html' in str(caught.value) and str(engine.path) in str(caught.value)

def test_missing_source_does_not_create_cache_directories(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    engine = Engine(source)
    with pytest.raises(TemplateNotFound):
        engine.render('missing.html')

    assert not engine.compiled_dir.exists()
    assert not (tmp_path / 'compiled_views.locks').exists()

def test_removing_source_and_cached_renderer_reports_template_not_found(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    page = source / 'page.html'
    page.write_text('cached', encoding='utf-8')
    view = Engine(source).get_template('page.html')
    renderer = Path(view.module.__file__)
    page.unlink()
    renderer.unlink()
    with pytest.raises(TemplateNotFound, match='page.html'):
        view.render()

def test_source_streams_do_not_compile_until_consumed(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    page = source / 'page.html'
    page.write_text('<if test="True">', encoding='utf-8')
    engine = Engine(source)
    view = engine.get_template('page.html')
    engine.iter_render('page.html').close()
    view.iter_render().close()
    assert not engine.compiled_dir.exists()
    stream = view.iter_render()
    with pytest.raises(TemplateSyntaxError):
        next(stream)

    page.write_text('fixed', encoding='utf-8')
    assert list(view.iter_render()) == ['fixed']

def test_engine_optimization_matches_build_and_separates_cache_entries(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    text = '<if test="False"><out value="missing" /></if><out value="value" />'
    (source / 'page.html').write_text(text, encoding='utf-8')
    modules = []
    for optimize in (False, True):
        engine = Engine(source, optimize=optimize)
        assert engine.optimize is optimize
        view = engine.get_template('page.html')
        assert view.render({'value': '<Ada>'}) == '&lt;Ada&gt;'
        code = Path(view.module.__file__).read_text(encoding='utf-8')
        compiled = compile_template(text, filename='page.html', optimize=optimize)
        assert code == compiled.code
        modules.append(Path(view.module.__file__).parent)

    assert modules[0] != modules[1]

def test_default_engine_reuses_the_current_cache_identity_without_sources(tmp_path, monkeypatch):
    source = tmp_path / 'templates'
    source.mkdir()
    page = source / 'page.html'
    page.write_text('cached', encoding='utf-8')
    cache = tmp_path / 'compiled_views'
    identity = dumps({
        'source': str(source.resolve()), 'compiler': VERSION, 'abi': ABI_VERSION,
        'compiler_revision': COMPILER_REVISION,
        'python_requires': PYTHON_REQUIRES,
        'escaping': ESCAPING_POLICY, 'application': '', 'minify_html': False, 'template_comments': False,
    }, sort_keys=True)
    key = 't_' + sha256((identity + '\0page.html').encode()).hexdigest()[:32]
    build(source, cache / key)
    page.unlink()
    monkeypatch.setattr('suzuri.cache.build', lambda *args, **kwargs: pytest.fail('Existing cache was not reused'))
    assert Engine(source).render('page.html') == 'cached'

@pytest.mark.parametrize('option', ['optimize', 'minify_html', 'template_comments'])
@pytest.mark.parametrize('value', [None, 1, 'yes'])
def test_compile_options_are_validated_before_parsing_or_writing(tmp_path, option, value):
    options = {option: value}
    with pytest.raises(TypeError, match=f'{option} must be a boolean'):
        Engine(tmp_path, **options)

    with pytest.raises(TypeError, match=f'{option} must be a boolean'):
        compile_template('<if test="True">', **options)

    with pytest.raises(TypeError, match=f'{option} must be a boolean'):
        build(tmp_path / 'missing', tmp_path / 'output', **options)

    assert list(tmp_path.iterdir()) == []

@pytest.mark.parametrize('arguments,message', [({'source': b'hello'}, 'source'), ({'source': 'hello', 'filename': 3}, 'filename')])
def test_compile_template_rejects_wrong_argument_types(arguments, message):
    with pytest.raises(TypeError, match=message + ' must be a string'):
        compile_template(**arguments)

@pytest.mark.parametrize('options,exception', [
    ({'application_version': None}, TypeError), ({'application_version': 1}, TypeError),
    ({'pattern': None}, TypeError), ({'pattern': ''}, ValueError),
    ({'pattern': '../*.html'}, ValueError), ({'pattern': '/absolute/*.html'}, ValueError),
    ({'pattern': 'C:*.html'}, ValueError), ({'pattern': '**\\*.html'}, ValueError),
    ({'pattern': '\0'}, ValueError),
])
def test_build_configuration_errors_precede_filesystem_errors(tmp_path, options, exception):
    with pytest.raises(exception):
        build(tmp_path / 'missing', tmp_path / 'output', **options)

    assert list(tmp_path.iterdir()) == []

def test_build_result_and_engine_use_the_same_template_names(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    for name in ('z.html', 'a.html'):
        (source / name).write_text(name, encoding='utf-8')

    class Directory:
        def __init__(self, path):
            self.path = path

        def __fspath__(self):
            return str(self.path)

    bundle = build(Directory(source), Directory(tmp_path / 'bundle'))
    engine = Engine(Directory(bundle.path))
    assert bundle.template_names == engine.template_names == ('a.html', 'z.html')
    assert engine.render('a.html') == 'a.html'

def test_located_errors_expose_the_same_fields(engine_factory):
    engine = engine_factory()
    with pytest.raises(TemplateRenderError) as runtime:
        engine.render('page.html')

    with pytest.raises(TemplateSyntaxError) as syntax:
        compile_template('<if test="True">', filename='page.html')

    for error in (runtime.value, syntax.value):
        assert str(error) == f'{error.filename}:{error.line}:{error.column}: {error.message}'

    assert isinstance(runtime.value.__cause__, KeyError)

def test_generator_callbacks_are_rejected(engine_factory, caplog):
    def callback(event):
        yield event

    async def async_callback(event):
        yield event

    class Observer:
        async def __call__(self, event):
            yield event

    for observer in (callback, async_callback, Observer()):
        with pytest.raises(TypeError, match='must not be a generator function'):
            engine_factory(on_render=observer)

    def hidden_callback(event):
        return callback(event)

    assert engine_factory(on_render=hidden_callback).render('page.html', {'value': 'ok'}) == 'ok'
    assert 'on_render must not return a generator' in caplog.text
    caplog.clear()
    assert engine_factory(on_render=lambda event: async_callback(event)).render('page.html', {'value': 'ok'}) == 'ok'
    assert 'on_render must not return an async generator' in caplog.text

def test_public_api_types_need_no_casts_or_bundle_specific_branches(tmp_path):
    caller = tmp_path / 'caller.py'
    caller.write_text('''from os import PathLike
from pathlib import Path
from typing import assert_type
from collections.abc import Generator, AsyncGenerator
from suzuri import Engine, Template, BuildResult, build

def use(directory: PathLike[str]) -> None:
    engine = Engine(directory, compiled_dir=directory, optimize=False)
    template = assert_type(engine.get_template('page.html'), Template)
    assert_type(template.render({'value': 'Ada'}), str)
    assert_type(engine.iter_render('page.html'), Generator[str, None, None]).close()
    assert_type(template.iter_render(), Generator[str, None, None]).close()
    assert_type(template.iter_render_async(), AsyncGenerator[str, None])
    result = assert_type(build(directory, directory), BuildResult)
    assert_type(result.template_names, tuple[str, ...])
    assert_type(engine.path, Path)

async def use_async(engine: Engine) -> None:
    assert_type(await engine.render_async('page.html'), str)
    assert_type(await engine.get_template('page.html').render_async(), str)
    stream = assert_type(engine.iter_render_async('page.html'), AsyncGenerator[str, None])
    await stream.aclose()
''', encoding='utf-8')
    checked = subprocess.run([executable, '-m', 'mypy', '--strict', '--no-incremental', str(caller)], capture_output=True, text=True)
    assert checked.returncode == 0, checked.stdout + checked.stderr
