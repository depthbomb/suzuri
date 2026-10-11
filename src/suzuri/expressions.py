from __future__ import annotations
from io import StringIO
from keyword import iskeyword
from bisect import bisect_left
from dataclasses import replace
from unicodedata import normalize
from suzuri.model import Expr, Attribute
from suzuri.runtime import PYTHON_TARGET
from tokenize import NL, OP, COMMENT, NEWLINE, TokenInfo, generate_tokens
from ast import AST, Name, Await, Yield, parse, Lambda, NamedExpr, YieldFrom, FormattedValue, iter_child_nodes

def _debug_replacements(
    expression: Expr, tokens: tuple[TokenInfo, ...], fields: list[FormattedValue],
) -> tuple[tuple[int, int, str], ...]:
    if not fields:
        return ()

    text = expression.text
    line_starts = [0, *(i + 1 for i, char in enumerate(text) if char == '\n'), len(text)]
    starts = [line_starts[token.start[0] - 1] + token.start[1] for token in tokens]
    ends = [line_starts[token.end[0] - 1] + token.end[1] for token in tokens]
    replacements = []
    for field in fields:
        index = bisect_left(starts, character_offset(expression, field.value, end=True))
        while tokens[index].type in (NL, NEWLINE, COMMENT) or tokens[index].string == ')':
            index += 1

        if tokens[index].type != OP or tokens[index].string != '=':
            continue

        equal = starts[index]
        index += 1
        while tokens[index].type in (NL, NEWLINE, COMMENT):
            index += 1

        end = starts[index]
        start = character_offset(expression, field) + 1
        # CPython omits comments from debug labels but preserves their newlines.
        chunks = []
        cursor = start
        for token_index in range(bisect_left(starts, start), index):
            if tokens[token_index].type == COMMENT:
                chunks.append(text[cursor:starts[token_index]])
                cursor = ends[token_index]

        chunks.append(text[cursor:end])
        label = ''.join(chunks)
        conversion = '!r' if tokens[index].string == '}' and field.conversion == ord('r') else ''
        replacements.append((start, start, repr(label) + '}{'))
        replacements.append((equal, end, conversion))

    return tuple(replacements)

def valid_name(name: str) -> bool:
    return (
        name.isidentifier()
        and not iskeyword(name)
        and name != '__debug__'
        and not name.startswith('_suzuri_')
        and normalize('NFKC', name) == name
    )

def parse_expression(attribute: Attribute) -> Expr:
    text = attribute.value.strip()
    leading = len(attribute.value) - len(attribute.value.lstrip())
    offsets = attribute.offsets[leading : leading + len(text) + 1]
    source = attribute.span.source
    if '\r' in text:
        # Match Python's universal newlines without losing original source offsets.
        positions = [index for index in range(len(text)) if not (text[index] == '\n' and index and text[index - 1] == '\r')]
        text = ''.join('\n' if text[index] == '\r' else text[index] for index in positions)
        offsets = (*(offsets[index] for index in positions), offsets[-1])

    if not text:
        raise source.error('Expected a Python expression', attribute.span.start)

    if len(text) > 10_000:
        raise source.error('Expression exceeds the 10,000 character limit', attribute.span.start)

    try:
        tree = parse(text, mode='eval', feature_version=PYTHON_TARGET)
    except SyntaxError as error:
        lines = text.split('\n')
        offset = sum(len(line) + 1 for line in lines[: (error.lineno or 1) - 1]) + (error.offset or 1) - 1
        raise source.error(f'Invalid Python expression: {error.msg}', offsets[max(0, min(offset, len(text)))]) from None
    except (ValueError, RecursionError) as error:
        raise source.error(f'Invalid Python expression: {error}', attribute.span.start) from None

    tokens = tuple(generate_tokens(StringIO(text).readline))
    delimiters = 0
    for token in tokens:
        if token.type == OP:
            delimiters += int(token.string in {'(', '[', '{'}) - int(token.string in {')', ']', '}'})
            if delimiters > 128:
                raise source.error('Expression delimiter nesting exceeds 128 levels', attribute.span.start)

    fields: list[FormattedValue] = []
    pending: list[tuple[AST, int]] = [(tree, 0)]
    while pending:
        node, depth = pending.pop()
        if depth > 64:
            raise source.error('Expression nesting exceeds 64 levels', attribute.span.start)

        pending.extend((child, depth + 1) for child in iter_child_nodes(node))
        if isinstance(node, FormattedValue):
            fields.append(node)

        if isinstance(node, (Await, Yield, YieldFrom, NamedExpr, Lambda)):
            raise source.error(
                'Await, yield, assignment expressions, and lambdas are unsupported', attribute.span.start
            )

        if isinstance(node, Name) and node.id.startswith('_suzuri_'):
            raise source.error('Names starting with _suzuri_ are reserved', attribute.span.start)

    expression = Expr(text, tree, attribute.span, offsets)
    if fields:
        return replace(expression, debug_replacements=_debug_replacements(expression, tokens, fields))

    return expression

def character_offset(expression: Expr, node: AST, *, end: bool = False) -> int:
    line = getattr(node, 'end_lineno' if end else 'lineno')
    column: int = getattr(node, 'end_col_offset' if end else 'col_offset')
    start = 0 if line == 1 else expression.line_starts[line - 1]
    if expression.text.isascii():
        return start + column

    prefix = expression.utf8_lines[line - 1][:column].decode('utf-8')
    return start + len(prefix)
