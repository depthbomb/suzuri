from __future__ import annotations
from re import compile as regex
from dataclasses import dataclass
from collections.abc import Iterator
from suzuri.model import Span, Source, Attribute

DIRECTIVES = frozenset({'out', 'if', 'elif', 'else', 'for', 'from', 'import', 'param', 'include', 'component'})
VOID_ELEMENTS = frozenset(
    {'area', 'base', 'br', 'col', 'embed', 'hr', 'img', 'input', 'link', 'meta', 'param', 'source', 'track', 'wbr'}
)
RAW_ELEMENTS = frozenset({'script', 'style', 'title', 'textarea', 'xmp', 'iframe', 'noembed', 'noframes', 'noscript'})
_TAG = regex(r'<(/?)([A-Za-z][A-Za-z0-9:_-]*)(?=[\s/>=])')
_ATTRIBUTE = regex(r'[A-Za-z_][A-Za-z0-9_-]*')
_COMPONENT_ATTRIBUTE = regex(r':?[A-Za-z_][A-Za-z0-9_-]*')
_ENTITY = regex(r'&(?:amp|lt|gt|quot|apos|#[0-9]+|#x[0-9a-fA-F]+);')
_REFERENCES = {'&amp;': '&', '&lt;': '<', '&gt;': '>', '&quot;': '"', '&apos;': "'"}
_DYNAMIC_ATTRIBUTE = regex(r'<\s*/?(?:' + '|'.join(sorted(DIRECTIVES)) + r')(?=[\s/>=])', flags=2)

@dataclass(frozen=True)
class Token:
    kind: str
    span: Span
    name: str = ''
    closing: bool = False
    self_closing: bool = False
    attributes_start: int = 0

    @property
    def text(self) -> str:
        return self.span.source.text[self.span.start : self.span.end]

def _decode(source: Source, start: int, end: int) -> Attribute:
    value = source.text[start:end]
    if '&' not in value:
        return Attribute(value, Span(source, start, end), tuple(range(start, end + 1)))

    result: list[str] = []
    offsets: list[int] = []
    cursor = start

    while cursor < end:
        char = source.text[cursor]
        if char == '&':
            match = _ENTITY.match(source.text, cursor, end)
            if match is None:
                raise source.error('Use a semicolon-terminated XML or numeric character reference', cursor)

            entity = match.group()
            if entity in _REFERENCES:
                char = _REFERENCES[entity]
            else:
                if len(entity) > 12:
                    raise source.error('Invalid Unicode character reference', cursor)

                codepoint = int(entity[3:-1], 16) if entity.startswith('&#x') else int(entity[2:-1])
                if not (codepoint in {9, 10, 13} or 32 <= codepoint <= 0x10FFFF) or 0xD800 <= codepoint <= 0xDFFF:
                    raise source.error('Invalid Unicode character reference', cursor)

                char = chr(codepoint)

            result.append(char)
            offsets.append(cursor)
            cursor = match.end()
        else:
            result.append(char)
            offsets.append(cursor)
            cursor += 1

    return Attribute(''.join(result), Span(source, start, end), (*offsets, end))

def directive_attributes(token: Token) -> dict[str, Attribute]:
    source = token.span.source
    text = source.text
    cursor = token.attributes_start
    end = token.span.end - 1
    if token.self_closing:
        end = text.rfind('/', cursor, end)

    attributes: dict[str, Attribute] = {}
    while cursor < end:
        before_space = cursor
        while cursor < end and text[cursor].isspace():
            cursor += 1

        if cursor == end:
            break

        if cursor == before_space:
            raise source.error('Directive attributes must be separated by whitespace', cursor)

        matcher = _COMPONENT_ATTRIBUTE if token.name == 'component' else _ATTRIBUTE
        match = matcher.match(text, cursor, end)
        if match is None:
            raise source.error('Expected a directive attribute name', cursor)

        name = match.group()
        cursor = match.end()
        while cursor < end and text[cursor].isspace():
            cursor += 1

        if cursor == end or text[cursor] != '=':
            raise source.error('Directive attributes require a quoted value', cursor)

        cursor += 1
        while cursor < end and text[cursor].isspace():
            cursor += 1

        if cursor == end or text[cursor] not in {'"', "'"}:
            raise source.error('Directive attributes require a quoted value', cursor)

        quote = text[cursor]
        start = cursor + 1
        cursor = text.find(quote, start, end)
        if cursor < 0:
            raise source.error('Unterminated directive attribute', start)

        if name in attributes:
            raise source.error(f'Duplicate attribute {name!r}', match.start())

        attributes[name] = _decode(source, start, cursor)
        cursor += 1

    return attributes

def tokenize(source: Source) -> Iterator[Token]:
    text = source.text
    cursor = 0
    while cursor < len(text):
        start = cursor
        if text.startswith('<!--', cursor):
            end = text.find('-->', cursor + 4)
            if end < 0:
                raise source.error('Unterminated HTML comment', cursor)

            cursor = end + 3
            yield Token('comment', Span(source, start, cursor))
            continue

        if text.startswith('<![CDATA[', cursor):
            end = text.find(']]>', cursor + 9)
            if end < 0:
                raise source.error('Unterminated CDATA section', cursor)

            cursor = end + 3
            yield Token('cdata', Span(source, start, cursor))
            continue

        match = _TAG.match(text, cursor)
        if match is None and not text[cursor : cursor + 9].lower() == '<!doctype':
            if text.startswith(('<!', '<?', '</'), cursor):
                raise source.error('Unsupported or malformed HTML markup', cursor)

            if (
                text[cursor] == '<'
                and cursor + 1 < len(text)
                and text[cursor + 1].isascii()
                and text[cursor + 1].isalpha()
            ):
                raise source.error('Malformed or unterminated HTML tag', cursor)

            cursor = text.find('<', cursor + 1)
            if cursor < 0:
                cursor = len(text)

            yield Token('text', Span(source, start, cursor))
            continue

        quote = ''
        cursor = match.end() if match else cursor + 9
        attributes_start = cursor
        while cursor < len(text):
            char = text[cursor]
            if quote:
                if char == quote:
                    quote = ''
            elif char in {'"', "'"}:
                quote = char
            elif char == '>':
                break
            elif char == '<':
                raise source.error('Unexpected < inside an HTML tag', cursor)

            cursor += 1

        if cursor == len(text):
            raise source.error('Unterminated HTML tag or quoted attribute', start)

        cursor += 1
        if match is None:
            yield Token('text', Span(source, start, cursor))
            continue

        name = match[2].lower()
        closing = bool(match[1])
        self_closing = text[start : cursor - 1].rstrip().endswith('/')
        kind = 'directive' if name in DIRECTIVES else 'html'
        if kind == 'directive' and match[2] != name:
            raise source.error('Directive names must be lowercase', start)

        if closing and text[attributes_start : cursor - 1].strip():
            raise source.error('Closing tags cannot have attributes', start)

        if kind == 'html' and _DYNAMIC_ATTRIBUTE.search(text[attributes_start : cursor - 1]):
            raise source.error('Directives cannot appear inside HTML attributes', start)

        if name == 'plaintext':
            raise source.error('The plaintext element is unsupported', start)

        if name in RAW_ELEMENTS and not closing:
            if self_closing:
                raise source.error(f'<{name}> requires an explicit end tag', start)

            close = regex(r'</' + name + r'\s*>', flags=2).search(text, cursor)
            if close is None:
                raise source.error(f'Unclosed <{name}> element', start)

            if name == 'script' and '<!--' in text[cursor : close.start()]:
                raise source.error(
                    'Legacy escaped script states are unsupported; remove <!-- from script content', cursor
                )

            cursor = close.end()
            yield Token('text', Span(source, start, cursor))
            continue

        yield Token(kind, Span(source, start, cursor), name, closing, self_closing, attributes_start)
