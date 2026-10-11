from html import escape
from ast import Constant
from itertools import groupby
from dataclasses import replace
from suzuri.model import Loop, Node, Text, Output, Scoped, Conditional

def _coalesce(nodes: list[Node]) -> tuple[Node, ...]:
    result: list[Node] = []
    for literal, group in groupby(nodes, key=lambda node: isinstance(node, Text)):
        if literal:
            chunks = [node for node in group if isinstance(node, Text)]
            value = ''.join(node.value for node in chunks)
            if value:
                result.append(Text(value, chunks[0].span))
        else:
            result.extend(group)

    return tuple(result)

def optimize_nodes(nodes: tuple[Node, ...], *, fold_outputs: bool = True) -> tuple[Node, ...]:
    result: list[Node] = []
    for node in nodes:
        if fold_outputs and isinstance(node, Output) and isinstance(node.expression.tree.body, Constant):
            node = Text(escape(str(node.expression.tree.body.value), quote=True), node.span)
        elif isinstance(node, Loop):
            node = Loop(node.name, node.iterable, optimize_nodes(node.body, fold_outputs=fold_outputs), node.span)
        elif isinstance(node, Scoped):
            node = replace(node, template=replace(node.template, body=optimize_nodes(node.template.body, fold_outputs=fold_outputs)))
        elif isinstance(node, Conditional):
            simplified: list[Node] = []
            kept = []
            for condition, body in node.branches:
                optimized = optimize_nodes(body, fold_outputs=fold_outputs)
                if condition is not None and isinstance(condition.tree.body, Constant):
                    if not condition.tree.body.value:
                        continue

                    condition = None

                kept.append((condition, optimized))
                if condition is None:
                    break

            if kept and kept[0][0] is None:
                simplified.extend(kept[0][1])
            elif kept:
                simplified.append(Conditional(tuple(kept), node.span))

            for item in simplified:
                result.append(item)
            continue

        result.append(node)

    return _coalesce(result)
