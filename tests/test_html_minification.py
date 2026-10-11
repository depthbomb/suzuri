from html.parser import HTMLParser
import pytest
from suzuri.html import minify_html

@pytest.mark.parametrize('source,expected', [
    ('\n  <main>\n  <p> Hello   world! </p>\n  <p> Again. </p>\n</main>\n', '<main><p>Hello world!</p><p>Again.</p></main>'),
    ('<p>Hello <strong>dear   reader</strong> and <em>welcome</em>.</p>', '<p>Hello <strong>dear reader</strong> and <em>welcome</em>.</p>'),
    ('<span>one</span> \n <span>two</span>', '<span>one</span> <span>two</span>'),
    ('<div><span>one</span> \n <span>two</span></div>', '<div><span>one</span> <span>two</span></div>'),
    ('<p><a> first </a><b> second </b></p>', '<p><a> first </a><b> second </b></p>'),
    ('<p>first<br>\n second</p>', '<p>first<br>second</p>'),
    ('<ul>\n <li> first </li>\n <li> second </li>\n</ul>', '<ul><li>first</li><li>second</li></ul>'),
    ('\r\n a\t  b\f c \r\n', 'a b c'),
    ('<p>a\u00a0\u00a0b &nbsp; &#32; c</p>', '<p>a\u00a0\u00a0b &nbsp; &#32; c</p>'),
    ('<p>a<!-- one --> <!-- two --> b</p>', '<p>a b</p>'),
    ('<p>a <!-- one --><!-- two -->b</p>', '<p>a b</p>'),
    ('1 < 2 and 3 > 2', '1 < 2 and 3 > 2'),
    ('<p> <<!-- remove -->b> </p>', '<p><<!---->b></p>'),
    ('<p>&not<!-- remove -->in;</p>', '<p>&not<!---->in;</p>'),
    ('<p>&#x<!-- remove -->41;</p>', '<p>&#x<!---->41;</p>'),
    ('<p>&<!-- remove -->amp;</p>', '<p>&<!---->amp;</p>'),
    ('<p>a<!-- remove -->b</p>', '<p>ab</p>'),
    ('<DIV\n CLASS = "card"\t DATA-X = \'abc\' > text </DIV  >', '<div class=card data-x=abc>text</div>'),
    ('<input disabled="false" checked=checked required="required" autofocus="autofocus" />', '<input disabled checked required autofocus>'),
    ('<option selected="selected" disabled="disabled"> value </option>', '<option selected disabled> value </option>'),
    ('<div hidden="until-found" draggable="false" contenteditable="true" aria-hidden="false"></div>', '<div hidden=until-found draggable=false contenteditable=true aria-hidden=false></div>'),
    ('<x-card disabled="false" checked="false" open="no"></x-card>', '<x-card disabled=false checked=false open=no></x-card>'),
    ('<div title="" data-empty=\'\' inert="inert" itemscope="itemscope"></div>', '<div title data-empty inert itemscope></div>'),
    ('<input value="a/b" />', '<input value=a/b>'),
    ('<img src="path/">', '<img src=path/>'),
    ('<img src=path/ >', '<img src=path/>'),
    ('<img src=path />', '<img src=path>'),
    ('<img src="path"/>', '<img src=path>'),
    ('<div data-text="&amp; &quot;" style=" color: red; \n white-space: normal "></div>', '<div data-text="&amp; &quot;" style=" color: red; \n white-space: normal "></div>'),
    ('<div title="\'quoted\'" data-x=\'"quoted"\'></div>', '<div title="\'quoted\'" data-x=\'"quoted"\'></div>'),
    ('<!DOCTYPE html>\n<html>\n<head>\n<meta charset="utf-8" />\n</head>\n<body> hi </body></html>', '<!DOCTYPE html><html><head><meta charset=utf-8></head><body>hi</body></html>'),
])
def test_html_minification(source, expected):
    assert minify_html(source, 'page.html') == expected
    assert minify_html(expected, 'page.html') == expected

@pytest.mark.parametrize('body', [
    '\r\n  // keep this newline\r\nconst value = "a  b";\n\t',
    '\n const template = `<div  title="a  b"> x </div>`;\n',
    '\n/* keep */\n.card::before { content: "<style> a  b"; }\n',
    '\n{"key": "a  b", "html": "<p> c </p>"}\n',
    'if (a < b && c > d) { run(); }\n',
])
@pytest.mark.parametrize('tag', ['script', 'style', 'SCRIPT', 'StYlE'])
def test_script_and_style_bodies_and_attributes_stay_exactly_intact(tag, body):
    raw = f'<{tag}  type="custom/data" data-code="a > b" >{body}</{tag} \t>'
    source = '<main>\n<p  class="x"> before   text </p>' + raw + '<p> after   text </p>\n</main>'
    expected = '<main><p class=x>before text</p>' + raw + '<p>after text</p></main>'
    assert minify_html(source, 'page.html') == expected

@pytest.mark.parametrize('preserved', [
    '<pre>\n first  line\n <span  class="x">  nested\n text </span>\n</pre>',
    '<pre>\n outer <pre>\n inner </pre>\n outer </pre>',
    '<code> a  b\n <code> c  d </code> e </code>',
    '<pre><script>const tag = "</pre>";\n</script>  tail\n</pre>',
    '<textarea>\n a  b\n <!-- keep --> </textarea>',
    '<svg viewBox="0 0 1 1"><!-- keep --><svg><text> a  b </text></svg>\n<text> c  d </text></svg>',
    '<svg><foreignObject><pre> a  b </pre></foreignObject><![CDATA[</svg>]]></svg>',
    '<math  display="inline"><mi> x  y </mi><!-- keep --></math>',
    '<noscript>\n<p  class="fallback"> a  b </p>\n</noscript>',
])
def test_nested_protected_content_is_unchanged(preserved):
    source = '<div>\n' + preserved + '\n</div>'
    assert minify_html(source, 'page.html') == '<div>' + preserved + '</div>'

@pytest.mark.parametrize('source', [
    '<div title="unfinished>',
    '<script>missing closing tag',
    '<style>missing closing tag',
    '<!-- unfinished',
    '<?unsupported processing instruction?>',
    '<div key=>text</div>',
    '<div key="value"missing-space="x">text</div>',
    '<div key=`bad`>text</div>',
    '<pre>unfinished',
    '<svg><![CDATA[unfinished',
    pytest.param('<div ' + 'attribute' * 10000 + '="unfinished', id='long-unclosed-attribute'),
])
def test_unsupported_markup_returns_original_input(source):
    source = '<!-- retain on fallback -->\n' + source
    assert minify_html(source, 'page.html') == source

@pytest.mark.parametrize('attribute', [
    'data-x="a/b"', 'data-x="a/"', 'data-x="&notin;"', 'data-x="&notit;"',
    'data-x="&#x20;"', 'data-x="&#34;"', 'data-x="a&#32;b"', 'data-x="&amp;quot;"',
    'data-x="a=b"', 'data-x="x > y"', 'data-x="a\tb"', 'data-x="a`b"',
    'data-x="a\'b"', 'data-x=""', 'data-x="café🐞"', 'data-x="a\u00a0b"',
    'value="path/"', 'value=path/', 'value=path /', 'aria-checked="false"',
])
def test_attribute_values_match_html_parser_after_minification(attribute):
    source = '<input ' + attribute + '>'
    attributes = []
    parser = HTMLParser()
    parser.handle_starttag = lambda tag, pairs: attributes.append([(key, value or '') for key, value in pairs])
    parser.feed(source)
    parser.feed(minify_html(source, 'page.html'))
    parser.close()
    assert len(attributes) == 2
    assert attributes[0] == attributes[1]

def test_large_repeated_markup_keeps_attributes_and_protected_regions():
    tag = '<div  class="card" data-value="a &amp; b"> text </div>'
    repeated = tag * 100
    protected = '<pre>\n  ' + tag * 10 + '\n</pre>'
    source = repeated + protected + repeated
    expected = '<div class=card data-value="a &amp; b">text</div>' * 100
    assert minify_html(source, 'large.html') == expected + protected + expected

def test_large_markup_keeps_fallback_after_many_different_tags():
    source = '<p  class="card"> text </p>' * 30
    source += ''.join(f'<div title="value-{index}"> x </div>' for index in range(300))
    source += '<div key=>invalid</div>'
    assert minify_html(source, 'large.html') == source
