import sys
import builtins
import subprocess
from json import loads, dumps
from pathlib import Path
from hashlib import sha256
from traceback import format_exception
from warnings import catch_warnings, simplefilter
from importlib.util import MAGIC_NUMBER, source_hash
from concurrent.futures import ThreadPoolExecutor
import pytest
from suzuri import build, Engine, ArtifactError, TemplateRenderError

@pytest.fixture
def source(tmp_path):
    root = tmp_path / 'templates'
    root.mkdir()
    (root / 'page.html').write_text('<p><out value="value" /></p>', encoding='utf-8')
    return root

def renderer_path(bundle, suffix='.py'):
    return bundle.path / (bundle.manifest['templates']['page.html']['module'] + suffix)

def forbid_renderer_compilation(monkeypatch):
    original = builtins.compile

    def checked(source, filename, *args, **kwargs):
        assert not Path(filename).name.startswith('t_'), 'Renderer source was compiled instead of loading bytecode'
        return original(source, filename, *args, **kwargs)

    monkeypatch.setattr(builtins, 'compile', checked)

def update_manifest(bundle, change):
    path = bundle.path / 'manifest.json'
    manifest = loads(path.read_text(encoding='utf-8'))
    change(manifest)
    path.write_text(dumps(manifest), encoding='utf-8')

@pytest.mark.parametrize('mode', ['eager', 'on_load'])
def test_bytecode_loads_without_compiling_and_supports_all_render_methods(source, tmp_path, monkeypatch, mode):
    bundle = build(source, tmp_path / 'bundle', bytecode=True)
    data = renderer_path(bundle, '.pyc').read_bytes()
    assert data[:4] == MAGIC_NUMBER
    assert int.from_bytes(data[4:8], 'little') == 3
    assert data[8:16] == source_hash(renderer_path(bundle).read_bytes())
    before = {path.name: path.read_bytes() for path in bundle.path.iterdir()}
    (source / 'page.html').unlink()
    forbid_renderer_compilation(monkeypatch)

    engine = Engine(bundle.path, verify_artifacts=mode)
    view = engine.get_template('page.html')
    assert view.render({'value': '<Ada>'}) == '<p>&lt;Ada&gt;</p>'
    assert ''.join(view.iter_render({'value': '<Ada>'}, chunk_size=1)) == '<p>&lt;Ada&gt;</p>'
    assert view.module.render_values(value='<Ada>') == '<p>&lt;Ada&gt;</p>'
    from asyncio import run

    assert run(view.render_async({'value': '<Ada>'})) == '<p>&lt;Ada&gt;</p>'

    async def stream():
        return ''.join([chunk async for chunk in view.iter_render_async({'value': '<Ada>'}, chunk_size=1)])

    assert run(stream()) == '<p>&lt;Ada&gt;</p>'
    assert {path.name: path.read_bytes() for path in bundle.path.iterdir()} == before

def test_bytecode_builds_are_deterministic_and_relocatable(source, tmp_path, monkeypatch):
    first = build(source, tmp_path / 'first', bytecode=True)
    second = build(source, tmp_path / 'second', bytecode=True)
    assert first.manifest == second.manifest
    assert {p.name: p.read_bytes() for p in first.path.iterdir()} == {p.name: p.read_bytes() for p in second.path.iterdir()}
    moved = tmp_path / 'relocated'
    first.path.rename(moved)
    engine = Engine(moved)
    forbid_renderer_compilation(monkeypatch)
    view = engine.get_template('page.html')
    module = view.module
    assert module.render.__code__.co_filename == str(moved / renderer_path(first).name)
    original = renderer_path(second).read_text(encoding='utf-8')
    for path in moved.iterdir():
        path.unlink()
    assert module.__spec__.loader.get_source(module.__name__) == original
    with catch_warnings():
        simplefilter('error', DeprecationWarning)
        with pytest.raises(TemplateRenderError) as error:
            view.render()
        trace = ''.join(format_exception(error.value))
    assert "_suzuri_context['value']" in trace
    assert error.value.filename == 'page.html'
    assert '.suzuri-' not in trace

def test_verified_bytecode_is_not_reread_before_execution(source, tmp_path, monkeypatch):
    bundle = build(source, tmp_path / 'bundle', bytecode=True)
    engine = Engine(bundle.path)
    renderer_path(bundle, '.pyc').write_bytes(b'changed after verification')
    renderer_path(bundle).write_text('raise AssertionError("changed source")', encoding='utf-8')
    forbid_renderer_compilation(monkeypatch)
    assert engine.render('page.html', {'value': 'original'}) == '<p>original</p>'

@pytest.mark.parametrize('mode', ['eager', 'on_load'])
@pytest.mark.parametrize('mutation', ['corrupt', 'missing'])
def test_bytecode_integrity_is_checked(source, tmp_path, mode, mutation):
    bundle = build(source, tmp_path / 'bundle', bytecode=True)
    if mutation == 'corrupt':
        renderer_path(bundle, '.pyc').write_bytes(b'corrupt')
    else:
        renderer_path(bundle, '.pyc').unlink()
    with pytest.raises(ArtifactError):
        Engine(bundle.path, verify_artifacts=mode).render('page.html', {'value': 'x'})

@pytest.mark.parametrize('field,value', [
    ('cache_tag', 'other-python'), ('magic_number', '00000000'),
    ('optimization', (sys.flags.optimize + 1) % 3),
])
def test_incompatible_bytecode_falls_back_to_source_without_writes(source, tmp_path, field, value):
    bundle = build(source, tmp_path / 'bundle', bytecode=True)
    # Invalid marshal data proves incompatible bytecode is never decoded.
    path = renderer_path(bundle, '.pyc')
    path.write_bytes(path.read_bytes()[:16] + b'invalid marshal payload')

    def change(manifest):
        manifest['bytecode'][field] = value
        manifest['artifacts'][path.name] = sha256(path.read_bytes()).hexdigest()

    update_manifest(bundle, change)
    before = {path.name: path.read_bytes() for path in bundle.path.iterdir()}
    assert Engine(bundle.path).render('page.html', {'value': '<ok>'}) == '<p>&lt;ok&gt;</p>'
    assert {path.name: path.read_bytes() for path in bundle.path.iterdir()} == before

@pytest.mark.parametrize('payload', [b'', b'garbage', b'N'])
def test_malformed_compatible_bytecode_raises_artifact_error(source, tmp_path, payload):
    bundle = build(source, tmp_path / 'bundle', bytecode=True)
    path = renderer_path(bundle, '.pyc')
    path.write_bytes(path.read_bytes()[:16] + payload)
    update_manifest(bundle, lambda manifest: manifest['artifacts'].update({path.name: sha256(path.read_bytes()).hexdigest()}))
    with pytest.raises(ArtifactError, match='Cannot load bytecode'):
        Engine(bundle.path).render('page.html', {'value': 'x'})

@pytest.mark.parametrize('metadata', [
    None, True, [], {}, {'cache_tag': 'cpython', 'magic_number': 'bad', 'optimization': 0},
    {'cache_tag': [], 'magic_number': MAGIC_NUMBER.hex(), 'optimization': 0},
    {'cache_tag': 'cpython', 'magic_number': MAGIC_NUMBER.hex(), 'optimization': True},
    {'cache_tag': 'cpython', 'magic_number': MAGIC_NUMBER.hex(), 'optimization': 3},
])
def test_invalid_bytecode_metadata_is_rejected(source, tmp_path, metadata):
    bundle = build(source, tmp_path / 'bundle', bytecode=True)
    update_manifest(bundle, lambda manifest: manifest.update(bytecode=metadata))
    with pytest.raises(ArtifactError, match='bytecode format'):
        Engine(bundle.path, verify_artifacts='on_load')

def test_missing_bytecode_manifest_entry_is_rejected(source, tmp_path):
    bundle = build(source, tmp_path / 'bundle', bytecode=True)
    update_manifest(bundle, lambda manifest: manifest['artifacts'].pop(renderer_path(bundle, '.pyc').name))
    with pytest.raises(ArtifactError, match='Missing template bytecode'):
        Engine(bundle.path)

@pytest.mark.parametrize('value', [None, 0, 1, 'yes', []])
def test_bytecode_option_requires_a_boolean(source, tmp_path, value):
    with pytest.raises(TypeError, match='bytecode must be a boolean'):
        build(source, tmp_path / 'bundle', bytecode=value)
    with pytest.raises(TypeError, match='bytecode must be a boolean'):
        Engine(source, bytecode=value)

def test_default_and_existing_source_only_bundles_still_work(source, tmp_path):
    bundle = build(source, tmp_path / 'bundle')
    assert not list(bundle.path.glob('*.pyc'))
    assert 'bytecode' not in bundle.manifest
    update_manifest(bundle, lambda manifest: manifest['options'].pop('bytecode'))
    assert Engine(bundle.path, bytecode=True).render('page.html', {'value': 'old'}) == '<p>old</p>'
    assert not list(bundle.path.glob('*.pyc'))

def test_source_bytecode_is_lazy_and_reused_across_engines(source, tmp_path, monkeypatch):
    cache = tmp_path / 'cache'
    engine = Engine(source, compiled_dir=cache, bytecode=True)
    assert engine.bytecode is True
    with pytest.raises(AttributeError):
        engine.bytecode = False
    view = engine.get_template('page.html')
    view.iter_render({'value': 'unused'}).close()
    assert not cache.exists()
    assert view.render({'value': 'first'}) == '<p>first</p>'
    assert len(list(cache.rglob('*.pyc'))) == 1
    (source / 'page.html').unlink()
    forbid_renderer_compilation(monkeypatch)
    with ThreadPoolExecutor(max_workers=4) as pool:
        results = list(pool.map(
            lambda i: Engine(source, compiled_dir=cache, bytecode=True).render('page.html', {'value': i}), range(8),
        ))
    assert results == [f'<p>{i}</p>' for i in range(8)]

def test_workers_can_compile_the_same_bytecode_cache(source, tmp_path):
    cache = tmp_path / 'cache'

    def render(value):
        result = subprocess.run([
            sys.executable, '-c',
            "from sys import argv; from suzuri import Engine; print(Engine(argv[1], compiled_dir=argv[2], bytecode=True).render('page.html', {'value': argv[3]}))",
            str(source), str(cache), str(value),
        ], capture_output=True, text=True, check=True, timeout=60)
        return result.stdout.strip()

    with ThreadPoolExecutor(max_workers=4) as pool:
        assert list(pool.map(render, range(4))) == [f'<p>{i}</p>' for i in range(4)]
    assert len(list(cache.rglob('*.pyc'))) == 1
    assert len(list(cache.glob('*/manifest.json'))) == 1
    assert not list(cache.glob('.*'))

@pytest.mark.parametrize('mutation', ['delete', 'corrupt'])
def test_source_cache_rebuilds_missing_or_corrupt_bytecode(source, tmp_path, mutation):
    cache = tmp_path / 'cache'
    engine = Engine(source, compiled_dir=cache, bytecode=True)
    assert engine.render('page.html', {'value': 'before'}) == '<p>before</p>'
    (source / 'page.html').write_text('rebuilt', encoding='utf-8')
    path = next(cache.rglob('*.pyc'))
    if mutation == 'delete':
        path.unlink()
    else:
        path.write_bytes(b'corrupt')
    assert engine.render('page.html') == 'rebuilt'
    assert Engine(source, compiled_dir=cache, bytecode=True).render('page.html') == 'rebuilt'

def test_bytecode_and_source_only_caches_are_separate(source, tmp_path):
    cache = tmp_path / 'cache'
    plain = Engine(source, compiled_dir=cache)
    compiled = Engine(source, compiled_dir=cache, bytecode=True)
    assert plain.render('page.html', {'value': 'plain'}) == '<p>plain</p>'
    (source / 'page.html').write_text('bytecode', encoding='utf-8')
    assert compiled.render('page.html') == 'bytecode'
    assert plain.render('page.html', {'value': 'cached'}) == '<p>cached</p>'
    assert len(list(cache.glob('*/manifest.json'))) == 2

@pytest.mark.parametrize('build_level', [0, 1, 2])
@pytest.mark.parametrize('load_level', [0, 1, 2])
def test_interpreter_optimization_levels_keep_their_semantics(source, tmp_path, build_level, load_level):
    (source / 'page.html').write_text('<out value="__debug__" />', encoding='utf-8')
    bundle = tmp_path / 'bundle'
    flags = {0: [], 1: ['-O'], 2: ['-OO']}
    subprocess.run([
        sys.executable, *flags[build_level], '-c',
        'from sys import argv; from suzuri import build; build(argv[1], argv[2], bytecode=True)',
        str(source), str(bundle),
    ], check=True, capture_output=True, text=True, timeout=60)
    result = subprocess.run([
        sys.executable, *flags[load_level], '-c',
        "from sys import argv; from suzuri import Engine; print(Engine(argv[1]).render('page.html'))", str(bundle),
    ], check=True, capture_output=True, text=True, timeout=60)
    assert result.stdout.strip() == str(load_level == 0)

def test_source_bytecode_caches_separate_optimization_levels(source, tmp_path):
    cache = tmp_path / 'cache'
    page = source / 'page.html'
    for level in (0, 1, 2, 0):
        page.write_text(str(level) + ':<out value="__debug__" />', encoding='utf-8')
        result = subprocess.run([
            sys.executable, *([] if level == 0 else ['-' + 'O' * level]), '-c',
            "from sys import argv; from suzuri import Engine; print(Engine(argv[1], compiled_dir=argv[2], bytecode=True).render('page.html'))",
            str(source), str(cache),
        ], check=True, capture_output=True, text=True, timeout=60)
        assert result.stdout.strip() == f'{level}:{level == 0}'
    assert len(list(cache.glob('*/manifest.json'))) == 3

@pytest.mark.parametrize('optimize', [False, True])
def test_composition_helpers_and_html_options_match_source_bundles(source, tmp_path, monkeypatch, optimize):
    (tmp_path / 'bytecode_helper.py').write_text('def upper(value): return value.upper()', encoding='utf-8')
    monkeypatch.syspath_prepend(str(tmp_path))
    (source / 'page.html').write_text(
        '<from module="bytecode_helper" import="upper" />'
        '<main  class="page"> <!-- remove me --> <include src="part.html" />'
        '<component name="card" :value="upper(value)" /></main>', encoding='utf-8',
    )
    (source / 'part.html').write_text('<out value="upper(value)" />', encoding='utf-8')
    (source / 'components').mkdir()
    (source / 'components' / 'card.html').write_text('<p><out value="value" /></p>', encoding='utf-8')
    options = {'optimize': optimize, 'minify_html': True, 'template_comments': True}
    plain = build(source, tmp_path / 'plain', **options)
    compiled = build(source, tmp_path / 'compiled', bytecode=True, **options)
    engine = Engine(compiled.path)
    assert 'bytecode_helper' not in sys.modules
    try:
        expected = Engine(plain.path).render('page.html', {'value': '<Ada>'})
        forbid_renderer_compilation(monkeypatch)
        assert engine.render('page.html', {'value': '<Ada>'}) == expected
        assert ''.join(engine.iter_render('page.html', {'value': '<Ada>'}, chunk_size=1)) == expected
        assert 'remove me' not in expected
        assert '&lt;ADA&gt;' in expected
    finally:
        sys.modules.pop('bytecode_helper', None)

def test_failed_bytecode_build_does_not_publish_partial_output(source, tmp_path, monkeypatch):
    def fail(*args, **kwargs):
        raise OSError('bytecode write failed')

    from importlib import import_module

    monkeypatch.setattr(import_module('suzuri.build'), 'compile_bytecode', fail)
    with pytest.raises(OSError, match='bytecode write failed'):
        build(source, tmp_path / 'bundle', bytecode=True)
    assert not (tmp_path / 'bundle').exists()
    assert not list(tmp_path.glob('.suzuri-*'))
