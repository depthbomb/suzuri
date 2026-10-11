import pytest
from suzuri import TemplateSyntaxError, compile_template

@pytest.mark.parametrize('source,message', [
    ('<out value=x />', 'quoted'),
    ('<out value="x" value="y" />', 'Duplicate'),
    ('<out other="x" />', 'missing'),
    ('<out value="x" other="y" />', 'unknown'),
    ('<out value="x">', 'self-closing'),
    ('<if test="True" />', 'opening and closing'),
    ('<IF test="True"></IF>', 'lowercase'),
    ('<for each="x" in="xs"></if>', 'Unexpected'),
    ('<if test="True">', 'Unclosed'),
    ('<else>x</else>', 'Orphan'),
    ('<if test="True">x</if><!--x--><else>y</else>', 'Orphan'),
    ('<if test="True">x</if><else>y</else><else>z</else>', 'Orphan'),
    ('<if test="True"><p></if></p>', 'context'),
    ('<p><if test="True"></p><p></if></p>', 'context'),
    ('<for each="x" in="xs"><div></for></div>', 'context'),
    ('<ul><li>one<li>two</ul>', 'Mismatched'),
    ('<div/>', 'explicit end tag'),
    ('<div>', 'Unclosed'),
    ('<p><b></p></b>', 'Mismatched'),
    ('<a href="<out value=\'url\' />">link</a>', 'attributes'),
    ('<svg><out value="value" /></svg>', 'SVG'),
    ('<math><if test="True">x</if></math>', 'MathML'),
    ('<out value="await fetch()" />', 'Await'),
    ('<out value="(x := 1)" />', 'assignment'),
    ('<out value="lambda x: x" />', 'lambdas'),
    ('<out value="[x async for x in things]" />', 'Async'),
    ('<out value="_suzuri_context" />', 'reserved'),
    ('<for each="class" in="xs"></for>', 'Invalid'),
    ('<out value="1 & 2" />', 'character reference'),
    ('<out value="\'&nbsp;\'" />', 'character reference'),
    ('<out value="\'&#0;\'" />', 'Unicode'),
    ('<out value="\'&#xD800;\'" />', 'Unicode'),
    ('<from module=".helpers" import="f" />', 'absolute'),
    ('<from module="helpers" import="*" />', 'Invalid'),
    ('<from module="helpers" import="f" /><param name="f" type="str" />', 'Duplicate'),
    ('<p>x</p><from module="helpers" import="f" />', 'precede'),
    ('<if test="True"><param name="name" type="str" /></if>', 'precede'),
    ('<from module="helpers" import="f" /><for each="f" in="values"></for>', 'shadow'),
    ('<param name="name" type="make_type()" />', 'calls'),
    ('<![CDATA[x]]>', 'CDATA'),
    ('<plaintext>x', 'plaintext'),
    ('<script><!-- <script> x </script> --> </script>', 'Legacy'),
    ('<include src="x.html" />', 'build'),
    ('<out', 'unterminated'),
    ('<param name="__debug__" type="bool" />', 'Invalid'),
    ('<param name="K" type="str" />', 'Invalid'),
    ('<from module="__future__" import="annotations" />', 'absolute'),
])
def test_rejected_syntax(source, message):
    with pytest.raises(TemplateSyntaxError, match=message):
        compile_template(source)

def test_expressions_support_comparisons_quotes_and_single_decoding(renderer):
    template = '<if test="x &lt; 3 and y > 1"><out value="&quot;&amp;lt;&quot;" /></if>'
    assert renderer(template).render({'x': 2, 'y': 3}) == '&amp;lt;'

def test_numeric_entities_and_multiline_expressions(renderer):
    source = '<out value="(\n    &#x31; +\n    2\n)" />'
    assert renderer(source).render({}) == '3'

def test_syntax_error_maps_to_original_entity_location():
    source = '🐞\n<out value="x &lt; )" />'
    with pytest.raises(TemplateSyntaxError) as error:
        compile_template(source, filename='unicode.html')

    assert error.value.filename == 'unicode.html'
    assert error.value.line == 2
    assert error.value.column == source.split('\n')[1].index(')') + 1

def test_expression_source_maps_use_unicode_character_offsets():
    source = '🐞<out value="café &lt; limit" />'
    compilation = compile_template(source)
    mapping = compilation.source_map['checker'][0]
    expression = compilation.checker[mapping['generated_start']:mapping['generated_end']]
    assert expression == 'café < limit'
    less_than = expression.index('<')
    assert mapping['source_offsets'][less_than] == source.index('&lt;')
    assert mapping['source_offsets'][0] == source.index('café')

def test_delimiter_nesting_has_room_for_generated_parentheses():
    source = '<out value="' + '(' * 150 + 'x' + ')' * 150 + '" />'
    with pytest.raises(TemplateSyntaxError, match='delimiter nesting'):
        compile_template(source)

def test_loop_nesting_limit_reports_a_template_error(renderer):
    opening = '<for each="i" in="[1]">\n'
    assert renderer(opening * 19 + 'x' + '</for>' * 19).render({}).endswith('x')
    with pytest.raises(TemplateSyntaxError, match='Loop nesting') as error:
        compile_template(opening * 20 + 'x' + '</for>' * 20, filename='deep.html')

    assert (error.value.filename, error.value.line) == ('deep.html', 20)
