"""Topologically reorder the declarations in a ``typespec-zod`` emission.

``typespec-zod`` (a pre-release sketch emitter, pinned ``0.0.0-68``) emits each
model as ``export const <name> = z.…;`` but orders cross-file declarations
wrong: a schema can reference another whose ``const`` is declared *later*. Zod
``const`` schemas are eagerly evaluated, so a forward reference is a temporal
dead zone — TypeScript rejects it (``TS2448``/``TS2454``, "used before
declaration"). The emitter's own collector claims topological order but
mis-sorts types split across files.

This pass parses the emission, builds the reference graph between the declared
names, and re-emits them dependency-first — each declaration's text preserved
verbatim. It is the Zod analogue of ``tools.schema.normalize`` (which works
around a json-schema emitter bug): an automated transform over generated code,
never a hand edit.

The reliable feature subset forbids recursion, so the graph is acyclic; a cycle
(which Zod can only express via ``z.lazy``, outside the subset) fails loud.
"""

from __future__ import annotations

import re

# Declarations start at column 0 with this exact prefix (the emitter's format).
_DECL_START = re.compile(r'^export const (\w+) =', re.MULTILINE)

# A referenced schema name appears as a bare identifier that is neither a member
# access (`z.string`) nor a property key (`name:`) nor inside the LHS. The
# negative lookbehind drops `.x`; the lookahead drops `x:` and partial-word
# matches. Remaining tokens that aren't declared names (zod methods) are filtered
# out against the declared-name set by the caller.
_REFERENCE = re.compile(r'(?<![.\w])([A-Za-z_]\w*)(?![\w:])')

# Non-code spans are stripped before the scan: a string/template literal or a
# comment whose text equals a declared schema name (an enum value `["widget"]`, or
# a `@doc`-derived JSDoc mentioning `widget`, alongside `export const widget`) would
# otherwise survive the name filter and forge a phantom edge — potentially a
# spurious cycle. Their contents are never references.
_NON_CODE = re.compile(
    r'"(?:[^"\\]|\\.)*"'  # double-quoted string
    r"|'(?:[^'\\]|\\.)*'"  # single-quoted string
    r'|`(?:[^`\\]|\\.)*`'  # template literal
    r'|//[^\n]*'  # line comment
    r'|/\*.*?\*/',  # block comment
    re.DOTALL,
)


def reorder(source: str) -> str:
    """Return the Zod emission with declarations sorted dependency-first.

    Args:
        source: The verbatim ``models.ts`` text from ``typespec-zod``.

    Returns:
        The same import preamble and declarations, with the declarations
        re-emitted so every schema appears after the ones it references. Ends
        with a single trailing newline.

    Raises:
        ValueError: If no declarations are found, or the reference graph has a
            cycle (which would require ``z.lazy``, outside the reliable subset).
    """
    starts = [(m.start(), m.group(1)) for m in _DECL_START.finditer(source)]
    if not starts:
        raise ValueError('no `export const` declarations found in Zod emission')

    preamble = source[: starts[0][0]].rstrip()
    bounds = [start for start, _ in starts] + [len(source)]
    order = [name for _, name in starts]
    # Each declaration spans from its `export const` to the next one (or EOF).
    decls = {name: source[bounds[i] : bounds[i + 1]].strip() for i, (_, name) in enumerate(starts)}

    names = set(order)
    deps = {
        name: {tok for tok in _REFERENCE.findall(_NON_CODE.sub('', text)) if tok in names and tok != name}
        for name, text in decls.items()
    }

    ordered = _toposort(order, deps)
    body = '\n\n'.join(decls[name] for name in ordered)
    return f'{preamble}\n\n{body}\n'


def _toposort(order: list[str], deps: dict[str, set[str]]) -> list[str]:
    """Dependencies-first order, stable in the original emission order.

    DFS post-order yields each node after its dependencies. Ties (independent
    nodes) keep their original relative order, so the output is deterministic
    for the freshness gate.
    """
    state: dict[str, int] = {}  # name -> 0 visiting, 1 done
    result: list[str] = []

    def visit(name: str, stack: tuple[str, ...]) -> None:
        if state.get(name) == 1:
            return
        if state.get(name) == 0:
            cycle = ' -> '.join((*stack, name))
            raise ValueError(
                f'cyclic schema reference in Zod emission: {cycle}; needs z.lazy (outside the reliable subset)'
            )
        state[name] = 0
        for dep in sorted(deps[name], key=order.index):
            visit(dep, (*stack, name))
        state[name] = 1
        result.append(name)

    for name in order:
        visit(name, ())
    return result
