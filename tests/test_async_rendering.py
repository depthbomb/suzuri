import asyncio
from threading import Event, get_ident
from contextvars import ContextVar
import pytest
from suzuri import build, Engine, TemplateRenderError, TemplateSyntaxError

@pytest.fixture
def views(tmp_path):
    source = tmp_path / 'templates'
    source.mkdir()
    (source / 'page.html').write_text('<for each="item" in="items"><out value="record(item)" /></for>', encoding='utf-8')
    return source, tmp_path / 'cache'

@pytest.mark.parametrize('target', ['engine', 'template', 'compiled'])
def test_async_apis_match_sync_output_and_propagate_context(views, target):
    source, cache = views
    events = []
    current_name = ContextVar('current_name', default='missing')
    if target == 'compiled':
        build(source, cache, minify_html=True, template_comments=True)
        engine = Engine(cache, chunk_size=1, on_render=events.append)
    else:
        engine = Engine(source, compiled_dir=cache, minify_html=True, template_comments=True, chunk_size=1, on_render=events.append)

    async def run():
        event_thread = get_ident()
        current_name.set('Ada')

        def record(item):
            assert get_ident() != event_thread
            return current_name.get() + str(item)

        context = {'items': [1, 2], 'record': record}
        if target == 'engine':
            output = await engine.render_async('page.html', context)
            stream = engine.iter_render_async('page.html', context)
        else:
            template = engine.get_template('page.html')
            output = await template.render_async(context)
            stream = template.iter_render_async(context)

        streamed = ''.join([chunk async for chunk in stream])
        assert output == streamed
        assert output.startswith('Ada1Ada2<!-- suzuri ')
        assert output.count('<!-- suzuri ') == 1

    asyncio.run(run())
    assert [(event.status, event.streaming) for event in events] == [('completed', False), ('completed', True)]

@pytest.mark.parametrize('streaming', [False, True])
def test_first_async_render_compiles_off_the_event_loop(views, monkeypatch, streaming):
    from suzuri.cache import build as real_build

    source, cache = views
    entered = Event()
    release = Event()

    def slow_build(*args, **kwargs):
        entered.set()
        assert release.wait(5), 'Compilation blocked the event loop'
        return real_build(*args, **kwargs)

    monkeypatch.setattr('suzuri.cache.build', slow_build)
    engine = Engine(source, compiled_dir=cache)

    async def run():
        context = {'items': [1], 'record': str}
        if streaming:
            stream = engine.iter_render_async('page.html', context)
            task = asyncio.create_task(anext(stream))
        else:
            task = asyncio.create_task(engine.render_async('page.html', context))

        try:
            assert await asyncio.to_thread(entered.wait, 5)
            assert not task.done()
        finally:
            release.set()

        assert await task == '1'
        if streaming:
            await stream.aclose()

    asyncio.run(run())

@pytest.mark.parametrize('streaming', [False, True])
def test_async_template_lookup_runs_off_the_event_loop(views, monkeypatch, streaming):
    source, cache = views
    engine = Engine(source, compiled_dir=cache)
    validate_name = engine._cache.validate_name
    event_thread = get_ident()
    checks = []

    def checked(name):
        assert get_ident() != event_thread
        checks.append(name)
        return validate_name(name)

    monkeypatch.setattr(engine._cache, 'validate_name', checked)

    async def run():
        context = {'items': [1], 'record': str}
        if streaming:
            stream = engine.iter_render_async('page.html', context)
            assert checks == []
            assert ''.join([chunk async for chunk in stream]) == '1'
        else:
            assert await engine.render_async('page.html', context) == '1'

    asyncio.run(run())
    assert checks

@pytest.mark.parametrize('target', ['engine', 'template'])
def test_async_buffered_context_is_copied_when_awaited(views, target):
    source, cache = views
    (source / 'page.html').write_text('<out value="pause()" /><out value="value" />', encoding='utf-8')
    entered = Event()
    release = Event()

    def pause():
        entered.set()
        assert release.wait(5), 'Consumer did not release the helper'
        return ''

    engine = Engine(source, compiled_dir=cache)
    context = {'value': 'before awaiting', 'pause': pause}

    async def run():
        pending = engine.render_async('page.html', context) if target == 'engine' else engine.get_template('page.html').render_async(context)
        context['value'] = 'when awaited'
        task = asyncio.create_task(pending)
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            context['value'] = 'after snapshot'
        finally:
            release.set()

        assert await task == 'when awaited'

    asyncio.run(run())

def test_unstarted_async_stream_does_not_compile_or_render(views):
    source, cache = views
    engine = Engine(source, compiled_dir=cache)

    async def run():
        stream = engine.iter_render_async('page.html')
        await stream.aclose()
        stream = engine.get_template('page.html').iter_render_async()
        await stream.aclose()

    asyncio.run(run())
    assert not cache.exists()

def test_async_stream_closure_stops_helpers_and_reports_lifecycle(views):
    source, cache = views
    calls = []
    events = []

    def record(item):
        calls.append(item)
        return str(item)

    engine = Engine(source, compiled_dir=cache, minify_html=True, template_comments=True, on_render=events.append)

    async def run():
        stream = engine.iter_render_async('page.html', {'items': range(5), 'record': record}, chunk_size=1)
        assert calls == []
        assert await anext(stream) == '0'
        assert calls == [0]
        await stream.aclose()
        await stream.aclose()

    asyncio.run(run())
    assert calls == [0]
    assert len(events) == 1
    assert (events[0].status, events[0].streaming, events[0].output_chars) == ('closed', True, 1)

@pytest.mark.parametrize('cancel_twice', [False, True])
def test_cancelled_stream_waits_for_inflight_helper_before_closing(views, cancel_twice):
    source, cache = views
    entered = Event()
    release = Event()
    events = []
    calls = []

    def record(item):
        entered.set()
        assert release.wait(5), 'Consumer did not release the helper'
        calls.append(item)
        return str(item)

    engine = Engine(source, compiled_dir=cache, chunk_size=1, on_render=events.append)

    async def run():
        stream = engine.iter_render_async('page.html', {'items': range(3), 'record': record})
        pending = asyncio.create_task(anext(stream))
        try:
            assert await asyncio.to_thread(entered.wait, 5)
            pending.cancel()
            await asyncio.sleep(0)
            if cancel_twice:
                pending.cancel()
                await asyncio.sleep(0)

            assert not pending.done()
            assert events == []
        finally:
            release.set()

        with pytest.raises(asyncio.CancelledError):
            await pending

        await stream.aclose()

    asyncio.run(run())
    assert calls == [0]
    assert len(events) == 1 and events[0].status == 'closed'

@pytest.mark.parametrize('streaming', [False, True])
def test_async_render_errors_keep_template_locations(views, streaming):
    source, cache = views
    engine = Engine(source, compiled_dir=cache)

    async def run():
        with pytest.raises(TemplateRenderError) as error:
            if streaming:
                async for _ in engine.iter_render_async('page.html'):
                    pass
            else:
                await engine.render_async('page.html')

        assert error.value.filename == 'page.html'
        assert isinstance(error.value.__cause__, KeyError)

    asyncio.run(run())

def test_async_compile_failure_can_be_retried(views):
    source, cache = views
    (source / 'page.html').write_text('<if test="True">', encoding='utf-8')
    engine = Engine(source, compiled_dir=cache)

    async def run():
        with pytest.raises(TemplateSyntaxError):
            async for _ in engine.iter_render_async('page.html'):
                pass

        (source / 'page.html').write_text('fixed', encoding='utf-8')
        assert await engine.render_async('page.html') == 'fixed'

    asyncio.run(run())

def test_concurrent_async_first_renders_compile_once(views, monkeypatch):
    from suzuri.cache import build as real_build

    source, cache = views
    calls = []

    def tracked(*args, **kwargs):
        calls.append(args)
        return real_build(*args, **kwargs)

    monkeypatch.setattr('suzuri.cache.build', tracked)
    engine = Engine(source, compiled_dir=cache)

    async def run():
        results = await asyncio.gather(*[
            engine.render_async('page.html', {'items': [number], 'record': str}) for number in range(20)
        ])
        assert results == [str(number) for number in range(20)]

    asyncio.run(run())
    assert len(calls) == 1
