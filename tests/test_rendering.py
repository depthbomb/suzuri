from html import escape
from types import SimpleNamespace
from concurrent.futures import ThreadPoolExecutor
import pytest
from suzuri import TemplateRenderError, compile_template

@pytest.mark.parametrize('source', [
    '', 'Hello\r\nworld', '<!DOCTYPE html><html><head><meta charset="utf-8"></head><body>Hi<br></body></html>',
    '<!-- <out value="missing" /> --><script>const x = \'<out value="missing" />\';</script>',
    '<style>a::after { content: "<if>" }</style><textarea><out value="missing" /></textarea>',
    '<product-card data-x="&amp; &unknown;">a & b</product-card>',
    '<svg viewBox="0 0 10 10"><path d="M0 0"/><![CDATA[<out value="x"/>]]></svg>',
    '1 < 2 and 2 > 1',
])
def test_literal_round_trip(renderer, source):
    module = renderer(source)
    assert module.render({}) == source
    assert ''.join(module.iter_render({}, chunk_size=1)) == source

@pytest.mark.parametrize('value', ['<&>"\'', 'café 🐞', None, 42, True])
def test_escape_every_dynamic_value(renderer, value):
    module = renderer('<p><out value="value" /></p>')
    expected = '<p>' + escape(str(value), quote=True) + '</p>'
    assert module.render({'value': value}) == expected
    assert ''.join(module.iter_render({'value': value})) == expected

def test_safe_html_protocol_does_not_bypass_escaping(renderer):
    class Markup:
        def __html__(self):
            return '<script>bad</script>'

        def __str__(self):
            return '<strong>hello</strong>'

    assert renderer('<out value="value" />').render({'value': Markup()}) == '&lt;strong&gt;hello&lt;/strong&gt;'

def test_branches_preserve_body_whitespace_and_discard_join_whitespace(renderer):
    module = renderer('<if test="x > 2"> high </if> \n <elif test="x == 2">equal</elif>\n<else> low </else>!')
    assert [module.render({'x': x}) for x in (3, 2, 1)] == [' high !', 'equal!', ' low !']

def test_nested_loops_restore_context_bindings(renderer):
    source = '<out value="item" /><for each="item" in="items"><out value="item.name" /><for each="item" in="item.children"><out value="item" /></for><out value="item.name" /></for><out value="item" />'
    module = renderer(source)
    context = {'item': 'outer', 'items': [SimpleNamespace(name='a', children=[1, 2])]}
    assert module.render(context) == 'outera12aouter'
    assert context['item'] == 'outer'

def test_empty_loop_and_missing_values_are_lazy(renderer):
    module = renderer('<if test="False"><out value="missing" /></if><for each="x" in="items"><out value="missing" /></for><out value="True or absent" />')
    assert module.render({'items': []}) == 'True'

def test_lookup_semantics_and_runtime_location(renderer):
    module = renderer('<p>\n<out value="user.name" />\n</p>')
    with pytest.raises(TemplateRenderError) as error:
        module.render({'user': {'name': 'dicts need brackets'}})

    assert isinstance(error.value.__cause__, AttributeError)
    assert (error.value.filename, error.value.line, error.value.column) == ('test.html', 2, 1)
    with pytest.raises(TemplateRenderError) as missing:
        module.render({})

    assert isinstance(missing.value.__cause__, KeyError)

def test_side_effects_are_not_hoisted_folded_or_reordered(renderer):
    calls = []

    def next_value():
        calls.append(len(calls))
        return calls[-1]

    module = renderer('<for each="i" in="range(3)"><out value="next_value()" /></for>')
    assert not calls
    assert module.render({'next_value': next_value}) == '012'
    assert module.render({'next_value': next_value}) == '345'

def test_comprehension_scopes_and_unicode_names(renderer):
    source = '<for each="café" in="groups"><out value="[x + café for x in values if x > 0]" /></for><out value="[x for x in values]" />'
    assert renderer(source).render({'groups': [2], 'values': [0, 1]}) == '[3][0, 1]'

def test_nested_comprehensions(renderer):
    module = renderer('<out value="[(x, y) for x in values for y in range(x)]" />')
    assert module.render({'values': [1, 2]}) == '[(1, 0), (2, 0), (2, 1)]'

def test_comprehension_assignment_targets_preserve_python_side_effects(renderer):
    holder = SimpleNamespace(value=0)
    module = renderer('<out value="[holder.value for holder.value in values]" />')
    assert module.render({'holder': holder, 'values': [1, 2]}) == '[1, 2]'
    assert holder.value == 2

@pytest.mark.parametrize('source', [
    '<out value="make()" />', '<if test="make()">yes</if>', '<for each="x" in="make()">x</for>',
])
def test_awaitables_are_rejected_without_coroutine_warning(renderer, source):
    async def make():
        return 'hello'

    with pytest.raises(TemplateRenderError, match='Awaitable'):
        renderer(source).render({'make': make})

def test_streaming_is_lazy_and_cancellation_stops_helpers(renderer):
    seen = []
    module = renderer('<for each="x" in="values"><out value="record(x)" /></for>')
    stream = module.iter_render({'values': range(4), 'record': lambda x: seen.append(x) or str(x)}, chunk_size=1)
    assert seen == []
    assert next(stream) == '0'
    stream.close()
    assert seen == [0]

def test_streaming_rejects_bad_chunk_size(renderer):
    with pytest.raises(ValueError, match='positive'):
        list(renderer('hello').iter_render({}, chunk_size=0))

def test_thread_safe_rendering_has_no_shared_context(renderer):
    module = renderer('<for each="x" in="range(5)"><out value="name" /></for>')
    with ThreadPoolExecutor(max_workers=8) as pool:
        assert list(pool.map(lambda i: module.render({'name': i}), range(50))) == [str(i) * 5 for i in range(50)]

def test_typed_render_values(renderer):
    module = renderer('<param name="name" type="str" /><p><out value="name" /></p>')
    assert module.render_values(name='A&B') == '<p>A&amp;B</p>'

@pytest.mark.parametrize('source,context', [
    ('<out value="42" />', {}),
    ('a<if test="False">bad</if><elif test="flag">ok</elif><else>else</else>z', {'flag': True}),
    ('<if test="True"><for each="x" in="values"><out value="x" /></for></if>', {'values': [1, 2]}),
    ('<if test="False">x</if>', {}),
    ('<out value="None" /><out value="True" />', {}),
])
def test_optimizer_matches_reference(renderer, source, context):
    expected = renderer(source, False).render(context)
    actual = renderer(source, True)
    assert actual.render(context) == expected
    assert ''.join(actual.iter_render(context)) == expected

def test_static_template_uses_constant_return():
    compiled = compile_template('<p><out value="42" /></p>')
    assert "return '<p>42</p>'" in compiled.code

def test_trailing_python_comments_do_not_swallow_generated_syntax(renderer):
    source = '<param name="x" type="int # input type" /><if test="x > 0 # positive"><out value="x # output" /></if>'
    module = renderer(source)
    assert module.render_values(x=3) == '3'
    assert ''.join(module.iter_render({'x': 3})) == '3'

def test_long_branch_chains_keep_flat_python_control_flow(renderer):
    source = '<if test="x == 0">zero</if>'
    source += ''.join(f'<elif test="x == {i}">{i}</elif>' for i in range(1, 150))
    source += '<else>other</else>'
    assert renderer(source).render({'x': 149}) == '149'
    assert renderer(source).render({'x': 150}) == 'other'

def test_elif_failure_reports_its_own_source_location(renderer):
    module = renderer('<if test="x">x</if>\n<elif test="missing">y</elif>')
    with pytest.raises(TemplateRenderError) as error:
        module.render({'x': False})

    assert error.value.line == 2
