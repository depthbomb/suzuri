from json import dumps
from re import compile as regex
from re import ASCII, IGNORECASE
from collections.abc import Mapping

_WHITESPACE = ' \t\r\n\f'
_SPACE = regex(r'[ \t\r\n\f]+')
# Possessive matching keeps malformed, long attributes from causing backtracking.
_TAG = regex(r'''<(/?)([A-Za-z][A-Za-z0-9:_-]*)(?=[ \t\r\n\f/>])(?:[^<>"']++|"[^"]*"|'[^']*')*>''')
_ATTRIBUTE = regex(r'''([^ \t\r\n\f/=>"'`<\x00]+)(?:[ \t\r\n\f]*=[ \t\r\n\f]*("[^"]*"|'[^']*'|[^ \t\r\n\f>"'`<=]+))?''')
_UNQUOTED = regex(r'''[^\x00-\x20\x7f"'`=<>]+''')
_ENTITY_TAIL = regex(r'&(?:[A-Za-z][A-Za-z0-9]*|\#[xX]?[0-9A-Fa-f]*)?$')
_DOCTYPE = regex(r'''<!DOCTYPE\b(?:[^<>"']++|"[^"]*"|'[^']*')*>''', ASCII | IGNORECASE)
_VOID = frozenset('area base br col embed hr img input link meta param source track wbr'.split())
_BLOCK = frozenset(
    'address article aside blockquote body br caption dd details dialog div dl dt fieldset figcaption figure '
    'footer form h1 h2 h3 h4 h5 h6 head header hgroup hr html li main menu nav ol p pre section table tbody '
    'td tfoot th thead tr ul'.split()
)
_PRESERVE = frozenset({'pre', 'code', 'svg', 'math'})
_RAW_END = {
    name: regex(r'</' + name + r'[ \t\r\n\f]*>', ASCII | IGNORECASE)
    for name in ('script', 'style', 'textarea', 'title', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript')
}
_GLOBAL_BOOLEAN = frozenset({'autofocus', 'inert', 'itemscope'})
_BOOLEAN = {
    'allowfullscreen': {'iframe'},
    'async': {'script'},
    'autoplay': {'audio', 'video'},
    'checked': {'input'},
    'controls': {'audio', 'video'},
    'default': {'track'},
    'defer': {'script'},
    'disabled': {'button', 'fieldset', 'input', 'link', 'optgroup', 'option', 'select', 'textarea'},
    'formnovalidate': {'button', 'input'},
    'ismap': {'img'},
    'loop': {'audio', 'video'},
    'multiple': {'input', 'select'},
    'muted': {'audio', 'video'},
    'nomodule': {'script'},
    'novalidate': {'form'},
    'open': {'details', 'dialog'},
    'playsinline': {'video'},
    'readonly': {'input', 'textarea'},
    'required': {'input', 'select', 'textarea'},
    'reversed': {'ol'},
    'selected': {'option'},
}
_COMMENT_ESCAPES = str.maketrans({char: f'\\u{ord(char):04x}' for char in '<>&-'})

class _Minifier:
    def __init__(self, *, marker_prefix: str = '', block_markers: frozenset[str] = frozenset()) -> None:
        self.parts: list[str] = []
        self.pending_space = False
        self.trim_space = True
        self.pending_comment = False
        self.last_text = ''
        self.marker_prefix = marker_prefix
        self.block_markers = block_markers
        self.dynamic_text = False

    def text(self, text: str) -> None:
        collapsed = _SPACE.sub(' ', text)
        content = collapsed.strip(' ')
        if content:
            if (self.pending_space or collapsed.startswith(' ')) and not self.trim_space:
                self.parts.append(' ')
            elif self.pending_comment and (
                self.dynamic_text or self.last_text.endswith('<') or _ENTITY_TAIL.search(self.last_text)
            ):
                # Deleting comments must not turn literal text into tags or entities.
                self.parts.append('<!---->')

            self.parts.append(content)
            self.last_text = content
            self.trim_space = False
            self.pending_comment = False

        self.pending_space = collapsed.endswith(' ')

    def markup(self, markup: str, *, block: bool = False) -> None:
        if self.pending_space and not self.trim_space and not block:
            self.parts.append(' ')

        self.parts.append(markup)
        self.trim_space = block
        self.pending_space = False
        self.pending_comment = False
        self.last_text = ''
        self.dynamic_text = False

    def marker(self, comment: str) -> None:
        if self.pending_comment and not self.pending_space and (
            self.dynamic_text or self.last_text.endswith('<') or _ENTITY_TAIL.search(self.last_text)
        ):
            self.parts.append('<!---->')

        block = comment in self.block_markers
        self.markup(comment, block=block)
        # The compiler removes this marker. Its rendered text is not known yet.
        self.dynamic_text = not block

    def run(self, html: str) -> str:
        cursor = 0
        length = len(html)
        protected: list[str] = []
        # Small fragments and raw blocks rarely reuse tags enough to offset a cache.
        tags: dict[str, str] | None = {} if html.count('</', 0, 4096) >= 16 else None
        while cursor < length:
            start = html.find('<', cursor)
            if start < 0:
                start = length

            if start > cursor:
                text = html[cursor:start]
                if protected:
                    self.parts.append(text)
                else:
                    self.text(text)

                cursor = start

            if cursor == length:
                break

            if html.startswith('<!--', cursor):
                end = html.find('-->', cursor + 4)
                if end < 0:
                    return html

                comment = html[cursor:end + 3]
                body = comment[4:-3].lstrip().lower()
                if protected:
                    self.parts.append(comment)
                elif self.marker_prefix and comment.startswith(self.marker_prefix):
                    self.marker(comment)
                elif body.startswith(('[', '!', '$', '/$', 'ko ', '/ko')):
                    self.markup(comment)
                else:
                    self.pending_comment = True

                cursor = end + 3
                continue

            if protected and html.startswith('<![CDATA[', cursor):
                end = html.find(']]>', cursor + 9)
                if end < 0:
                    return html

                self.parts.append(html[cursor:end + 3])
                cursor = end + 3
                continue

            tag = _TAG.match(html, cursor)
            if tag is None:
                doctype = _DOCTYPE.match(html, cursor)
                if doctype:
                    self.markup(doctype[0], block=True)
                    cursor = doctype.end()
                    continue

                following = html[cursor + 1:cursor + 2]
                if following and (following in '!/?' or following.isascii() and following.isalpha()):
                    return html

                if protected:
                    self.parts.append('<')
                else:
                    self.text('<')

                cursor += 1
                continue

            name = tag[2].lower()
            closing = bool(tag[1])
            end = tag.end()
            raw_end = _RAW_END.get(name)
            if raw_end is not None and not closing:
                close = raw_end.search(html, end)
                if close is None:
                    return html

                # Copy raw elements in one slice, including every byte of their bodies.
                end = close.end()
                if protected:
                    self.parts.append(html[cursor:end])
                else:
                    self.markup(html[cursor:end])
            elif protected:
                self.parts.append(tag[0])
                if closing and name == protected[-1]:
                    protected.pop()
                    if not protected:
                        self.trim_space = name in _BLOCK
                elif not closing and name in _PRESERVE and not tag[0][:-1].rstrip(_WHITESPACE).endswith('/'):
                    protected.append(name)
            elif name in _PRESERVE and not closing:
                self.markup(tag[0], block=name in _BLOCK)
                if not tag[0][:-1].rstrip(_WHITESPACE).endswith('/'):
                    protected.append(name)
            else:
                literal = tag[0]
                if tags is None:
                    compact = _minify_tag(literal, name, closing, tag.end(2) - cursor)
                else:
                    compact = tags.get(literal)
                    if compact is None:
                        compact = _minify_tag(literal, name, closing, tag.end(2) - cursor)
                        if compact is not None and len(tags) < 256:
                            tags[literal] = compact

                if compact is None:
                    return html

                self.markup(compact, block=name in _BLOCK)

            cursor = end

        return html if protected else ''.join(self.parts)

def _minify_tag(tag: str, name: str, closing: bool, attributes_start: int) -> str | None:
    attributes = tag[attributes_start:-1]
    if closing:
        return f'</{name}>' if not attributes.strip(_WHITESPACE) else None

    if not attributes:
        return '<' + name + '>'

    parts = ['<' + name]
    cursor = 0
    for attribute in _ATTRIBUTE.finditer(attributes):
        separator = attributes[cursor:attribute.start()]
        if not separator or separator.strip(_WHITESPACE):
            return None

        key = attribute[1].lower()
        value = attribute[2]
        if value is None or key in _GLOBAL_BOOLEAN or name in _BOOLEAN.get(key, ()):
            parts.append(' ' + key)
        else:
            unquoted = value[1:-1] if value[0] in {'"', "'"} else value
            if not unquoted:
                parts.append(' ' + key)
            elif _UNQUOTED.fullmatch(unquoted):
                parts.append(' ' + key + '=' + unquoted)
            else:
                parts.append(' ' + key + '=' + value)

        cursor = attribute.end()

    tail = attributes[cursor:].strip(_WHITESPACE)
    if tail not in ('', '/'):
        return None

    if tail and name not in _VOID:
        parts.append(' /')

    parts.append('>')
    return ''.join(parts)

def minify_html(html: str, filename: str) -> str:
    """Minify HTML whitespace and attributes without processing embedded languages.

    Raw elements (including script/style), pre, code, SVG and MathML stay intact.
    Normal text uses collapsed HTML whitespace; CSS changing display or white-space
    can require disabling minification. Unsupported markup is returned unchanged.
    """
    if '<' not in html:
        return _SPACE.sub(' ', html).strip(' ')

    return _Minifier().run(html)

def template_comment(metadata: Mapping[str, object]) -> str:
    """Encode metadata as JSON that cannot end or nest an HTML comment."""
    payload = dumps(dict(metadata), ensure_ascii=True, separators=(',', ':')).translate(_COMMENT_ESCAPES)
    return f'<!-- suzuri {payload} -->'
