import json
from pathlib import Path
from dataclasses import FrozenInstanceError
from concurrent.futures import ThreadPoolExecutor
import pytest
from suzuri import build, Engine, RenderEvent, ArtifactError, TemplateRenderError

@pytest.fixture
def bundle(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'hello.html').write_text('<out value="name" /><out value="suffix" />', encoding='utf-8')
    (source / 'items.html').write_text('<for each="item" in="items"><out value="record(item)" /></for>', encoding='utf-8')
    return build(source, tmp_path / 'views', application_version='app-1')

def artifact(bundle, name, suffix='.py'):
    return bundle.path / (bundle.manifest['templates'][name]['module'] + suffix)

def test_defaults_are_copied_read_only_and_overridden_per_render(bundle):
    defaults = {'name': 'Ada', 'suffix': '!'}
    engine = Engine(bundle.path, default_context=defaults)
    defaults['name'] = 'changed'
    template = engine.get_template('hello.html')
    assert engine.render('hello.html') == 'Ada!'
    context = {'name': '<Reese>'}
    assert template.render(context) == '&lt;Reese&gt;!'
    assert ''.join(template.iter_render({'suffix': '?'})) == 'Ada?'
    assert context == {'name': '<Reese>'}
    assert dict(engine.default_context) == {'name': 'Ada', 'suffix': '!'}
    with pytest.raises(TypeError):
        engine.default_context['name'] = 'not allowed'

    with pytest.raises(AttributeError):
        engine.chunk_size = 1

def test_defaults_are_shallow_and_contexts_are_independent_between_threads(bundle):
    items = [1, 2]
    engine = Engine(bundle.path, default_context={'suffix': '!', 'items': items, 'record': str})
    items.append(3)
    assert engine.render('items.html') == '123'
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda i: engine.render('hello.html', {'name': i}), range(30)))

    assert results == [f'{i}!' for i in range(30)]
    assert 'name' not in engine.default_context

def test_stream_context_is_merged_when_created(bundle):
    engine = Engine(bundle.path, default_context={'name': 'default', 'suffix': '!'}, chunk_size=1)
    context = {'name': 'original'}
    stream = engine.iter_render('hello.html', context)
    context['name'] = 'changed'
    assert ''.join(stream) == 'original!'

def test_stream_without_defaults_snapshots_context_when_created(bundle):
    context = {'name': 'original', 'suffix': '!'}
    stream = Engine(bundle.path).iter_render('hello.html', context)
    context['name'] = 'changed'
    assert ''.join(stream) == 'original!'

@pytest.mark.parametrize('observer', [False, True])
def test_chunk_size_is_inherited_and_can_be_overridden(bundle, observer):
    events = []
    engine = Engine(bundle.path, chunk_size=2, on_render=events.append if observer else None)
    template = engine.get_template('items.html')
    context = {'items': range(5), 'record': str}
    assert list(template.iter_render(context)) == ['01', '23', '4']
    assert list(engine.iter_render('items.html', context, chunk_size=3)) == ['012', '34']
    assert engine.chunk_size == 2

@pytest.mark.parametrize('size,exception', [(0, ValueError), (-1, ValueError), (True, TypeError), (1.5, TypeError), ('2', TypeError)])
def test_invalid_chunk_sizes_are_rejected_before_rendering(bundle, size, exception):
    with pytest.raises(exception, match='positive integer'):
        Engine(bundle.path, chunk_size=size)

    with pytest.raises(exception, match='positive integer'):
        Engine(bundle.path).iter_render('items.html', chunk_size=size)

@pytest.mark.parametrize('options,exception,message', [
    ({'preload': 'yes'}, TypeError, 'boolean'),
    ({'verify_artifacts': False}, TypeError, 'verify_artifacts'),
    ({'verify_artifacts': 'never'}, ValueError, 'verify_artifacts'),
    ({'default_context': []}, TypeError, 'mapping'),
    ({'default_context': {1: 'value'}}, TypeError, 'string keys'),
    ({'on_render': 42}, TypeError, 'callable'),
])
def test_invalid_configuration_is_rejected(bundle, options, exception, message):
    with pytest.raises(exception, match=message):
        Engine(bundle.path, **options)

@pytest.mark.parametrize('mode', ['eager', 'on_load'])
def test_preload_resolves_helpers_at_startup(tmp_path, mode):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'helper.html').write_text('<from module="suzuri_missing_test_helper" import="value" /><out value="value" />')
    result = build(source, tmp_path / 'views')
    engine = Engine(result.path, verify_artifacts=mode)
    with pytest.raises(ModuleNotFoundError, match='suzuri_missing_test_helper'):
        _ = engine.get_template('helper.html').module

    with pytest.raises(ModuleNotFoundError, match='suzuri_missing_test_helper'):
        Engine(result.path, verify_artifacts=mode, preload=True)

@pytest.mark.parametrize('mode', ['eager', 'on_load'])
def test_preload_caches_all_templates_without_rendering(bundle, mode, monkeypatch):
    events = []
    engine = Engine(bundle.path, preload=True, verify_artifacts=mode, on_render=events.append)
    assert engine.preload is True and engine.verify_artifacts == mode
    assert engine.template_names == ('hello.html', 'items.html')
    assert not events
    monkeypatch.setattr('builtins.exec', lambda *args: pytest.fail('Cached templates must not execute their modules again'))
    template = engine.get_template('hello.html')
    assert engine.get_template('hello.html') is template
    assert template.render({'name': 'Ada', 'suffix': '!'}) == 'Ada!'
    assert len(events) == 1

def test_on_load_reads_only_requested_artifacts_once(bundle, monkeypatch):
    reads = []
    read_bytes = Path.read_bytes

    def tracked_read(path):
        reads.append(path.name)
        return read_bytes(path)

    monkeypatch.setattr(Path, 'read_bytes', tracked_read)
    engine = Engine(bundle.path, verify_artifacts='on_load')
    assert reads == []
    assert engine.template_names == ('hello.html', 'items.html')
    template = engine.get_template('hello.html')
    assert reads == []
    _ = template.module
    expected = {'__init__.py', 'py.typed'} | {artifact(bundle, 'hello.html', suffix).name for suffix in ('.py', '.pyi', '_check.py', '.map.json')}
    assert set(reads) == expected and len(reads) == len(expected)
    reads.clear()
    assert engine.render('hello.html', {'name': 'Ada', 'suffix': '!'}) == 'Ada!'
    assert reads == []
    _ = engine.get_template('items.html').module
    assert set(reads) == {artifact(bundle, 'items.html', suffix).name for suffix in ('.py', '.pyi', '_check.py', '.map.json')}

@pytest.mark.parametrize('suffix', ['.py', '.pyi', '_check.py', '.map.json'])
def test_on_load_verifies_selected_template_and_its_sidecars(bundle, suffix):
    engine = Engine(bundle.path, verify_artifacts='on_load')
    damaged = artifact(bundle, 'items.html', suffix)
    original = damaged.read_bytes()
    damaged.write_bytes(b'corrupted')
    assert engine.render('hello.html', {'name': 'Ada', 'suffix': '!'}) == 'Ada!'
    with pytest.raises(ArtifactError, match='hash mismatch'):
        _ = engine.get_template('items.html').module

    with pytest.raises(ArtifactError, match='hash mismatch'):
        Engine(bundle.path, preload=True, verify_artifacts='on_load')

    with pytest.raises(ArtifactError, match='hash mismatch'):
        Engine(bundle.path)

    damaged.write_bytes(original)
    assert engine.render('items.html', {'items': [1], 'record': str}) == '1'

def test_on_load_verifies_shared_artifacts_and_reports_missing_files(bundle):
    engine = Engine(bundle.path, verify_artifacts='on_load')
    (bundle.path / 'py.typed').write_bytes(b'corrupted')
    with pytest.raises(ArtifactError, match='hash mismatch'):
        _ = engine.get_template('hello.html').module

    (bundle.path / 'py.typed').write_bytes(b'')
    artifact(bundle, 'hello.html').unlink()
    with pytest.raises(ArtifactError, match='Cannot load template'):
        _ = engine.get_template('hello.html').module

@pytest.mark.parametrize('mode', ['eager', 'on_load'])
def test_verified_bytes_cannot_be_replaced_before_execution(bundle, mode, monkeypatch):
    engine = Engine(bundle.path, verify_artifacts=mode)
    if mode == 'on_load':
        read_bytes = Path.read_bytes

        def replace_after_read(path):
            content = read_bytes(path)
            if path == artifact(bundle, 'hello.html'):
                path.write_bytes(b'raise RuntimeError("unverified code executed")')

            return content

        monkeypatch.setattr(Path, 'read_bytes', replace_after_read)
    else:
        artifact(bundle, 'hello.html').write_bytes(b'raise RuntimeError("unverified code executed")')

    assert engine.render('hello.html', {'name': 'safe', 'suffix': '!'}) == 'safe!'

def test_on_load_keeps_manifest_and_application_checks_eager(bundle):
    with pytest.raises(ArtifactError, match='dependency version'):
        Engine(bundle.path, application_version='wrong', verify_artifacts='on_load')

    path = bundle.path / 'manifest.json'
    manifest = json.loads(path.read_text())
    manifest['runtime_abi'] = 999
    path.write_text(json.dumps(manifest))
    with pytest.raises(ArtifactError, match='incompatible'):
        Engine(bundle.path, verify_artifacts='on_load')

def test_on_load_caches_once_during_concurrent_first_access(bundle):
    engine = Engine(bundle.path, verify_artifacts='on_load')
    with ThreadPoolExecutor(max_workers=8) as pool:
        templates = list(pool.map(lambda _: engine.get_template('hello.html'), range(30)))

    assert all(template is templates[0] for template in templates)

def test_buffered_events_report_output_or_original_error(bundle):
    events = []
    engine = Engine(bundle.path, on_render=events.append)
    assert engine.on_render == events.append
    assert engine.render('hello.html', {'name': '<Ada>', 'suffix': '!'}) == '&lt;Ada&gt;!'
    event = events.pop()
    assert isinstance(event, RenderEvent)
    assert (event.template_name, event.status, event.streaming, event.output_chars, event.error) == ('hello.html', 'completed', False, 12, None)
    assert event.duration_seconds >= 0
    with pytest.raises(FrozenInstanceError):
        event.status = 'error'

    with pytest.raises(TemplateRenderError) as failure:
        engine.get_template('hello.html').render()

    assert len(events) == 1
    assert (events[0].status, events[0].output_chars, events[0].error) == ('error', 0, failure.value)

@pytest.mark.parametrize('outcome', ['completed', 'closed', 'error'])
def test_stream_events_are_lazy_and_report_one_final_outcome(bundle, outcome):
    events = []
    calls = []

    def record(item):
        calls.append(item)
        if item == 1 and outcome == 'error':
            raise ValueError('render failed')

        return str(item)

    engine = Engine(bundle.path, chunk_size=1, on_render=events.append)
    stream = engine.iter_render('items.html', {'items': range(3), 'record': record})
    assert not calls and not events
    assert next(stream) == '0'
    assert not events
    failure = None
    if outcome == 'completed':
        assert list(stream) == ['1', '2']
    elif outcome == 'error':
        with pytest.raises(TemplateRenderError) as caught:
            list(stream)

        failure = caught.value
    else:
        stream.close()
        assert calls == [0]

    stream.close()
    assert list(stream) == []
    assert len(events) == 1
    event = events[0]
    assert (event.template_name, event.status, event.streaming, event.error) == ('items.html', outcome, True, failure)
    assert event.output_chars == (3 if outcome == 'completed' else 1)
    assert event.duration_seconds >= 0

def test_unstarted_stream_has_no_event(bundle):
    events = []
    stream = Engine(bundle.path, on_render=events.append).iter_render('items.html')
    stream.close()
    assert events == []

@pytest.mark.parametrize('streaming', [False, True])
def test_callback_errors_are_logged_without_masking_render_result(bundle, caplog, streaming):
    def callback(event):
        raise RuntimeError('observer failed')

    template = Engine(bundle.path, on_render=callback).get_template('hello.html')

    def render(context):
        return ''.join(template.iter_render(context)) if streaming else template.render(context)

    assert render({'name': 'Ada', 'suffix': '!'}) == 'Ada!'
    with pytest.raises(TemplateRenderError):
        render({})

    assert caplog.text.count('on_render callback failed') == 2

def test_async_callbacks_are_rejected_and_hidden_coroutines_are_closed(bundle, caplog):
    async def callback(event):
        pass

    class Observer:
        async def __call__(self, event):
            pass

    for observer in (callback, Observer()):
        with pytest.raises(TypeError, match='synchronous'):
            Engine(bundle.path, on_render=observer)

    coroutines = []

    def hidden_async(event):
        coroutine = callback(event)
        coroutines.append(coroutine)
        return coroutine

    engine = Engine(bundle.path, on_render=hidden_async)
    assert engine.render('hello.html', {'name': 'Ada', 'suffix': '!'}) == 'Ada!'
    assert coroutines[0].cr_frame is None
    assert 'on_render callback failed' in caplog.text

def test_rendering_without_observer_does_not_measure_time(bundle, monkeypatch):
    monkeypatch.setattr('suzuri.rendering.perf_counter', lambda: pytest.fail('No timing needed'))
    engine = Engine(bundle.path)
    context = {'name': 'Ada', 'suffix': '!'}
    assert engine.render('hello.html', context) == 'Ada!'
    assert ''.join(engine.iter_render('hello.html', context)) == 'Ada!'
