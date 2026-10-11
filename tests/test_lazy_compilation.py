import sys
import subprocess
from json import loads
from pathlib import Path
from shutil import rmtree
from concurrent.futures import ThreadPoolExecutor
import pytest
from suzuri import Engine, Template, BuildError, TemplateSyntaxError, TemplateRenderError

@pytest.fixture
def views(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'page.html').write_text('<p><out value="name" /></p>', encoding='utf-8')
    return source, tmp_path / 'compiled_views'

def test_compilation_waits_for_render_and_only_builds_requested_view(views):
    source, cache = views
    (source / 'broken.html').write_text('<if test="True">', encoding='utf-8')
    engine = Engine(source, compiled_dir=cache)
    view = engine.get_template('page.html')
    assert isinstance(view, Template)
    assert view is engine.get_template('page.html')
    assert engine.template_names == ('broken.html', 'page.html')
    assert not cache.exists()
    assert view.render({'name': '<Ada>'}) == '<p>&lt;Ada&gt;</p>'
    manifests = list(cache.rglob('manifest.json'))
    assert len(manifests) == 1
    assert list(loads(manifests[0].read_text())['templates']) == ['page.html']
    assert Path(view.module.__file__).is_relative_to(cache)

def test_default_cache_is_next_to_sources(views):
    source, cache = views
    engine = Engine(source)
    assert engine.compiled_dir == cache
    assert engine.render('page.html', {'name': 'Ada'}) == '<p>Ada</p>'
    assert cache.is_dir()

def test_cached_renderers_survive_restarts_without_reading_or_parsing_sources(views, monkeypatch):
    source, cache = views
    engine = Engine(source, compiled_dir=cache)
    assert engine.render('page.html', {'name': 'first'}) == '<p>first</p>'
    (source / 'page.html').write_text('changed', encoding='utf-8')
    monkeypatch.setattr('suzuri.cache.build', lambda *args, **kwargs: pytest.fail('Must reuse the compiled view'))
    original_read = Path.read_bytes

    def read(path):
        assert path.suffix != '.html', 'Cached views must not reread their source'
        return original_read(path)

    monkeypatch.setattr(Path, 'read_bytes', read)
    assert engine.render('page.html', {'name': 'second'}) == '<p>second</p>'
    assert Engine(source, compiled_dir=cache).render('page.html', {'name': 'third'}) == '<p>third</p>'
    (source / 'page.html').unlink()
    assert Engine(source, compiled_dir=cache).render('page.html', {'name': 'fourth'}) == '<p>fourth</p>'

@pytest.mark.parametrize('deleted', ['cache', 'view', 'renderer', 'manifest'])
def test_deleting_cache_invalidates_existing_handles_and_other_engines(views, deleted):
    source, cache = views
    first = Engine(source, compiled_dir=cache)
    second = Engine(source, compiled_dir=cache)
    view = first.get_template('page.html')
    assert view.render({'name': 'old'}) == second.render('page.html', {'name': 'old'}) == '<p>old</p>'
    original_module = view.module
    renderer = Path(original_module.__file__)
    (source / 'page.html').write_text('<b><out value="name" /></b>', encoding='utf-8')
    if deleted == 'cache':
        rmtree(cache)
    elif deleted == 'view':
        rmtree(renderer.parent)
    elif deleted == 'renderer':
        renderer.unlink()
    else:
        (renderer.parent / 'manifest.json').unlink()

    assert view.render({'name': 'new'}) == '<b>new</b>'
    assert view.module is not original_module
    assert second.render('page.html', {'name': 'new'}) == '<b>new</b>'

def test_include_changes_wait_for_cache_deletion_and_keep_metadata(views):
    source, cache = views
    (source / 'page.html').write_text('<main><include src="part.html" /></main>', encoding='utf-8')
    part = source / 'part.html'
    part.write_text('<p>old</p>', encoding='utf-8')
    engine = Engine(source, compiled_dir=cache, minify_html=True, template_comments=True)
    assert engine.render('page.html').startswith('<main><p>old</p></main><!-- suzuri ')
    part.write_text('<p>new <out value="missing" /></p>', encoding='utf-8')
    assert Engine(source, compiled_dir=cache, minify_html=True, template_comments=True).render('page.html') == engine.render('page.html')
    metadata = loads(engine.get_template('page.html').module.__suzuri_template_comment__[len('<!-- suzuri '):-4])
    assert metadata['includes'] == ['part.html']
    rmtree(cache)
    with pytest.raises(TemplateRenderError) as error:
        engine.render('page.html')

    assert error.value.filename == 'part.html'

def test_configuration_and_source_roots_have_separate_caches(views, tmp_path):
    source, cache = views
    (source / 'page.html').write_text('<p  class="card"> hello  world </p>', encoding='utf-8')
    other_source = tmp_path / 'other'
    other_source.mkdir()
    (other_source / 'page.html').write_text('other', encoding='utf-8')
    plain = Engine(source, compiled_dir=cache)
    compact = Engine(source, compiled_dir=cache, minify_html=True, template_comments=True)
    versioned = Engine(source, compiled_dir=cache, minify_html=True, template_comments=True, application_version='v2')
    other = Engine(other_source, compiled_dir=cache)
    assert plain.render('page.html') == '<p  class="card"> hello  world </p>'
    assert compact.render('page.html').startswith('<p class=card>hello world</p><!-- suzuri ')
    assert '"application_version":"v2"' in versioned.render('page.html')
    assert other.render('page.html') == 'other'
    assert len(list(cache.rglob('manifest.json'))) == 4

def test_concurrent_engines_compile_once(views, monkeypatch):
    from suzuri.cache import build

    source, cache = views
    builds = []

    def tracked(*args, **kwargs):
        builds.append(args)
        return build(*args, **kwargs)

    monkeypatch.setattr('suzuri.cache.build', tracked)
    engines = [Engine(source, compiled_dir=cache) for _ in range(12)]
    with ThreadPoolExecutor(max_workers=12) as workers:
        results = list(workers.map(lambda engine: engine.render('page.html', {'name': 'Ada'}), engines))

    assert results == ['<p>Ada</p>'] * 12
    assert len(builds) == 1

def test_concurrent_processes_share_a_complete_cache(views, tmp_path):
    source, cache = views
    count = tmp_path / 'compilations'
    script = '''
import sys
import importlib
from pathlib import Path
from suzuri import Engine
cache_module = importlib.import_module('suzuri.cache')
original = cache_module.build
def tracked(*args, **kwargs):
    with Path(sys.argv[3]).open('ab') as handle:
        handle.write(b'1')
    return original(*args, **kwargs)
cache_module.build = tracked
assert Engine(sys.argv[1], compiled_dir=sys.argv[2]).render('page.html', {'name': 'Ada'}) == '<p>Ada</p>'
'''

    def run(_):
        return subprocess.run([sys.executable, '-c', script, str(source), str(cache), str(count)], capture_output=True, text=True, timeout=30)

    with ThreadPoolExecutor(max_workers=4) as workers:
        results = list(workers.map(run, range(4)))

    assert all(result.returncode == 0 for result in results), [result.stderr for result in results]
    assert count.read_bytes() == b'1'
    assert len(list(cache.rglob('manifest.json'))) == 1

def test_failed_compilation_can_be_fixed_and_retried(views):
    source, cache = views
    page = source / 'page.html'
    page.write_text('<if test="True">', encoding='utf-8')
    view = Engine(source, compiled_dir=cache).get_template('page.html')
    with pytest.raises(TemplateSyntaxError):
        view.render()

    assert not list(cache.rglob('manifest.json'))
    page.write_text('fixed', encoding='utf-8')
    assert view.render() == 'fixed'
    assert not list(cache.glob('.*'))

def test_corrupt_cached_code_is_recompiled_before_execution(views):
    source, cache = views
    view = Engine(source, compiled_dir=cache).get_template('page.html')
    assert view.render({'name': 'Ada'}) == '<p>Ada</p>'
    renderer = Path(view.module.__file__)
    renderer.write_text('raise RuntimeError("must not execute")', encoding='utf-8')
    assert view.render({'name': 'safe'}) == '<p>safe</p>'

@pytest.mark.parametrize('name', ['', '../escape.html', '/absolute.html', 'C:/absolute.html', 'parts\\page.html', './page.html', 'a//b.html'])
def test_names_cannot_escape_source_root(views, name):
    source, cache = views
    with pytest.raises(ValueError):
        Engine(source, compiled_dir=cache).render(name)

    assert not cache.exists()

def test_glob_characters_in_template_names_are_literal(views):
    source, cache = views
    (source / 'page[1].html').write_text('literal', encoding='utf-8')
    (source / 'page1.html').write_text('other', encoding='utf-8')
    assert Engine(source, compiled_dir=cache).render('page[1].html') == 'literal'

def test_source_and_cache_cannot_overlap(views):
    source, _ = views
    for cache in (source, source / 'cache', source.parent):
        with pytest.raises(BuildError, match='overlap'):
            Engine(source, compiled_dir=cache)

def test_minified_stream_stays_lazy_and_does_not_minify_dynamic_values(views):
    source, cache = views
    (source / 'page.html').write_text('<for each="item" in="items"><out value="record(item)" /></for>', encoding='utf-8')
    calls = []

    def record(item):
        calls.append(item)
        return item

    engine = Engine(source, compiled_dir=cache, minify_html=True, template_comments=True)
    stream = engine.iter_render('page.html', {'items': [' a\n  b ', 'next'], 'record': record}, chunk_size=1)
    assert calls == []
    assert next(stream) == ' a\n  b '
    assert calls == [' a\n  b ']
    assert ' a\n  b ' + ''.join(stream) == engine.render('page.html', {'items': [' a\n  b ', 'next'], 'record': str})

def test_preload_is_an_explicit_opt_in_to_early_compilation(views):
    source, cache = views
    (source / 'other.html').write_text('other', encoding='utf-8')
    events = []
    Engine(source, compiled_dir=cache, preload=True, on_render=events.append)
    assert len(list(cache.rglob('manifest.json'))) == 2
    assert events == []
