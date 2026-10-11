from json import loads, dumps
from hashlib import sha256
from html.parser import HTMLParser
from concurrent.futures import ThreadPoolExecutor
import pytest
from suzuri import build, Engine, ArtifactError, TemplateRenderError

@pytest.fixture
def make_bundle(tmp_path):
    def create(source, *, includes=None, application_version='app-1'):
        templates = tmp_path / 'templates'
        templates.mkdir()
        (templates / 'page.html').write_bytes(source.encode('utf-8'))
        for name, text in (includes or {}).items():
            path = templates / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(text.encode('utf-8'))

        return build(templates, tmp_path / 'views', application_version=application_version)

    return create

def comment_metadata(html):
    start = html.rindex('<!-- suzuri ')
    assert html.endswith(' -->')
    return loads(html[start + len('<!-- suzuri '):-len(' -->')])

@pytest.mark.parametrize('option', ['minify_html', 'template_comments'])
@pytest.mark.parametrize('value', [None, 0, 1, 'yes', [], {}])
def test_output_options_require_booleans(make_bundle, option, value):
    bundle = make_bundle('hello')
    with pytest.raises(TypeError, match=f'{option} must be a boolean'):
        Engine(bundle.path, **{option: value})

def test_output_options_default_off_and_are_read_only(make_bundle):
    source = '<!-- note -->\r\n<p  title="two  spaces" >hello\nworld</p>'
    bundle = make_bundle(source)
    engine = Engine(bundle.path)
    assert engine.minify_html is False and engine.template_comments is False
    assert engine.render('page.html') == source
    assert ''.join(engine.iter_render('page.html', chunk_size=1)) == source
    for option in ('minify_html', 'template_comments'):
        with pytest.raises(AttributeError):
            setattr(engine, option, True)

@pytest.mark.parametrize('observer', [False, True])
@pytest.mark.parametrize('size', [1, 7, 8192])
def test_minification_is_shared_by_engine_template_and_streams(make_bundle, observer, size):
    source = '<!-- discard --><p\n  title="two  spaces"\t data-code=x >Hello <out value="name" /></p  >'
    expected = '<p title="two  spaces" data-code=x>Hello &lt;Ada&gt; Lovelace</p>'
    bundle = make_bundle(source)
    events = []
    engine = Engine(bundle.path, minify_html=True, chunk_size=size, on_render=events.append if observer else None)
    template = engine.get_template('page.html')
    context = {'name': '<Ada>\n  Lovelace'}
    assert engine.minify_html is True
    assert engine.render('page.html', context) == expected
    assert template.render(context) == expected
    assert ''.join(engine.iter_render('page.html', context)) == expected
    chunks = list(template.iter_render(context, chunk_size=3))
    assert all(0 < len(chunk) <= 3 for chunk in chunks)
    assert ''.join(chunks) == expected
    if observer:
        assert len(events) == 4
        assert all(event.output_chars == len(expected) and event.status == 'completed' for event in events)
        assert [event.streaming for event in events] == [False, False, True, True]

@pytest.mark.parametrize('source', [
    '<pre>\n a  b\r\n <b> c </b>\n</pre>',
    '<textarea>\n a  b &amp;\n<!-- literal -->\n</textarea>',
    '<script type="text/javascript">// keep newline\nlet x = "a  b";\n</script>',
    '<style>p::before { content: "a  b"; }\n/* keep */\n</style>',
    '<title>a  b\n &amp;</title>',
    '<xmp><!-- literal -->\n  text</xmp><iframe>\n  fallback</iframe>',
    '<noscript><!-- retain -->\n  <p  class="x" >fallback</p></noscript>',
    '<div title=" a\n\t b > \' c " data-x=unquoted>text</div>',
    '<svg  viewBox="0 0 1 1"><!-- retain --><text>a  b</text><![CDATA[ a  b ]]></svg>',
    '<math  display="inline"><!-- retain --><mi>x</mi></math>',
    '<!--[if IE]><script src="legacy.js"></script><![endif]-->',
    '<!--! license --><!-- ko if: ready --><p>ok</p><!-- /ko --><!--$--><!--/$-->',
])
def test_minification_preserves_sensitive_content(make_bundle, source):
    engine = Engine(make_bundle(source).path, minify_html=True)
    assert engine.render('page.html') == source
    assert ''.join(engine.iter_render('page.html', chunk_size=1)) == source

@pytest.mark.parametrize('source,expected', [
    ('<!-- ordinary --><p>ok</p><!-- ordinary -->', '<p>ok</p>'),
    ('a<!-- ordinary -->b', 'ab'),
    ('&am<!-- first --><!-- second -->p;', '&am<!---->p;'),
    ('<<!-- ordinary -->img src=x>', '<<!---->img src=x>'),
    ('<!-- only a comment -->', ''),
    ('', ''),
])
def test_comment_removal_does_not_create_entities_or_markup(make_bundle, source, expected):
    engine = Engine(make_bundle(source).path, minify_html=True)
    assert engine.render('page.html') == expected
    assert ''.join(engine.iter_render('page.html', chunk_size=1)) == expected

def test_unsupported_rendered_markup_is_left_unchanged(make_bundle):
    bundle = make_bundle('<!-- retain on fallback --><div key=><out value="name" /></div>')
    engine = Engine(bundle.path, minify_html=True, template_comments=True)
    body = engine.render('page.html', {'name': 'unterminated'})
    assert body.startswith('<!-- retain on fallback --><div key=>unterminated</div><!-- suzuri ')
    assert ''.join(engine.iter_render('page.html', {'name': 'unterminated'}, chunk_size=1)) == body

@pytest.mark.parametrize('minify', [False, True])
@pytest.mark.parametrize('mode', ['eager', 'on_load'])
def test_metadata_comes_from_bundle_and_is_added_after_minification(make_bundle, minify, mode):
    source = '<!DOCTYPE html><!-- remove me --><p  >Hi <include src="parts/name.html" /></p>'
    bundle = make_bundle(source, includes={'parts/name.html': '<out value="name" />'})
    events = []
    engine = Engine(bundle.path, minify_html=minify, template_comments=True, verify_artifacts=mode, on_render=events.append)
    body = engine.render('page.html', {'name': 'context-secret'})
    assert body.startswith('<!DOCTYPE html>')
    assert ('<!-- remove me -->' in body) is not minify
    expected = {
        'name': 'page.html',
        'source_sha256': sha256(source.encode('utf-8')).hexdigest(),
        'includes': ['parts/name.html'],
        'compiler_version': '0.1.0',
        'application_version': 'app-1',
    }
    assert comment_metadata(body) == expected
    assert 'context-secret' not in body[body.rindex('<!-- suzuri '):]
    assert body.count('<!-- suzuri ') == 1
    assert ''.join(engine.get_template('page.html').iter_render({'name': 'context-secret'}, chunk_size=1)) == body
    assert all(event.output_chars == len(body) and event.status == 'completed' for event in events)
    part = engine.render('parts/name.html', {'name': 'part'})
    assert comment_metadata(part)['name'] == 'parts/name.html'
    assert comment_metadata(part)['includes'] == []

def test_metadata_cannot_escape_html_comment(make_bundle):
    hostile = 'a--><!--<script>alert(1)</script><!--\r\n\u2028🐞'
    bundle = make_bundle('<p>ok</p>', application_version=hostile)
    path = bundle.path / 'manifest.json'
    manifest = loads(path.read_text(encoding='utf-8'))
    manifest['templates'][hostile] = manifest['templates'].pop('page.html')
    manifest['templates'][hostile]['dependencies'] = [hostile, hostile + '-include']
    manifest['sources'][hostile] = manifest['sources'].pop('page.html')
    path.write_text(dumps(manifest), encoding='utf-8')
    body = Engine(bundle.path, template_comments=True).render(hostile)
    metadata = comment_metadata(body)
    assert metadata['name'] == metadata['application_version'] == hostile
    assert metadata['includes'] == [hostile + '-include']
    comments = []
    tags = []
    parser = HTMLParser()
    parser.handle_comment = comments.append
    parser.handle_starttag = lambda name, attrs: tags.append(name)
    parser.feed(body)
    parser.close()
    assert tags == ['p']
    assert len(comments) == 1
    assert '--' not in comments[0] and '<' not in comments[0] and '\n' not in comments[0]

@pytest.mark.parametrize('field,value', [('sources', []), ('compiler_version', 1), ('application_version', {})])
def test_malformed_metadata_is_reported_only_when_requested(make_bundle, field, value):
    bundle = make_bundle('hello')
    path = bundle.path / 'manifest.json'
    manifest = loads(path.read_text(encoding='utf-8'))
    manifest[field] = value
    path.write_text(dumps(manifest), encoding='utf-8')
    assert Engine(bundle.path).render('page.html') == 'hello'
    with pytest.raises(ArtifactError, match='Malformed template metadata'):
        Engine(bundle.path, template_comments=True).render('page.html')

@pytest.mark.parametrize('minify', [False, True])
@pytest.mark.parametrize('outcome', ['completed', 'closed', 'error'])
def test_output_processing_stream_lifecycle(make_bundle, minify, outcome):
    bundle = make_bundle('<for each="item" in="items"><out value="record(item)" /></for>')
    events = []
    calls = []

    def record(item):
        calls.append(item)
        if item == 1 and outcome == 'error':
            raise ValueError('helper failed')

        return item

    engine = Engine(bundle.path, minify_html=minify, template_comments=True, chunk_size=1, on_render=events.append)
    context = {'items': range(3), 'record': record}
    stream = engine.iter_render('page.html', context)
    assert calls == events == []
    output = []
    error = None
    if outcome == 'error':
        with pytest.raises(TemplateRenderError) as failure:
            for chunk in stream:
                output.append(chunk)

        error = failure.value
        assert ''.join(output) == ('' if minify else '0')
    else:
        output.append(next(stream))
        assert calls == ([0, 1, 2] if minify else [0])
        if outcome == 'completed':
            output.extend(stream)
            assert comment_metadata(''.join(output))['name'] == 'page.html'

    stream.close()
    assert len(events) == 1
    event = events[0]
    assert (event.status, event.streaming, event.output_chars, event.error) == (
        outcome, True, len(''.join(output)), error,
    )

@pytest.mark.parametrize('minify', [False, True])
def test_closing_unstarted_processed_stream_does_no_work(make_bundle, minify):
    bundle = make_bundle('<out value="missing" />')
    events = []
    stream = Engine(bundle.path, minify_html=minify, template_comments=True, on_render=events.append).iter_render('page.html')
    stream.close()
    assert events == []

def test_empty_output_still_gets_metadata(make_bundle):
    engine = Engine(make_bundle('<!-- removed -->').path, minify_html=True, template_comments=True)
    result = engine.render('page.html')
    assert result.startswith('<!-- suzuri ')
    assert ''.join(engine.iter_render('page.html')) == result

def test_instances_and_concurrent_renders_do_not_share_output_options(make_bundle):
    source = '<!-- removed --><p  ><out value="name" /></p>'
    bundle = make_bundle(source)
    plain = Engine(bundle.path)
    compact = Engine(bundle.path, minify_html=True, template_comments=True, preload=True)
    with ThreadPoolExecutor(max_workers=8) as pool:
        results = list(pool.map(lambda value: compact.render('page.html', {'name': value}), range(20)))

    assert all(result.startswith(f'<p>{index}</p><!-- suzuri ') for index, result in enumerate(results))
    assert plain.render('page.html', {'name': 'plain'}) == '<!-- removed --><p  >plain</p>'
