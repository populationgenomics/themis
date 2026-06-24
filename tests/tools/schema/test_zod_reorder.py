"""Unit tests for the Zod reorder pass in tools/schema/zod_reorder.py.

The pass works around typespec-zod emitting ``export const`` declarations in a
non-dependency order, which Zod's eager evaluation rejects (TS2448). These cover
it in isolation — feeding emitter-shaped ``.ts`` strings through ``reorder``, no
Node toolchain needed — mirroring how test_normalize and test_regen cover the
sibling passes.
"""

from __future__ import annotations

import textwrap

import pytest

from tools.schema import zod_reorder

# The emitter's verbatim demo output: catalogue (references widget) is declared
# before widget, and widget (references colour) before colour — the bug.
_DEMO_EMITTED = textwrap.dedent("""\
    import { z } from "zod";

    export const colour = z.enum(["red", "green", "blue"]);

    export const catalogue = z.object({
      widgets: z.array(widget),
    });

    export const widget = z.object({
      name: z.string(),
      colour: colour,
    });
""")


def test_reorders_dependencies_first() -> None:
    expected = textwrap.dedent("""\
        import { z } from "zod";

        export const colour = z.enum(["red", "green", "blue"]);

        export const widget = z.object({
          name: z.string(),
          colour: colour,
        });

        export const catalogue = z.object({
          widgets: z.array(widget),
        });
    """)
    assert zod_reorder.reorder(_DEMO_EMITTED) == expected


def test_reorder_is_idempotent() -> None:
    once = zod_reorder.reorder(_DEMO_EMITTED)
    assert zod_reorder.reorder(once) == once


def test_already_ordered_input_is_unchanged() -> None:
    source = textwrap.dedent("""\
        import { z } from "zod";

        export const a = z.string();

        export const b = z.object({ a: a });
    """)
    assert zod_reorder.reorder(source) == source


def test_preserves_import_preamble_verbatim() -> None:
    # A multi-line preamble (e.g. several imports) is kept as-is above the decls.
    source = textwrap.dedent("""\
        import { z } from "zod";
        import { foo } from "./foo.js";

        export const a = z.string();
    """)
    out = zod_reorder.reorder(source)
    assert out.startswith('import { z } from "zod";\nimport { foo } from "./foo.js";\n\n')


def test_independent_declarations_keep_emission_order() -> None:
    # No edges between b and a: the original relative order is preserved (stable),
    # so the output is deterministic for the freshness gate.
    source = textwrap.dedent("""\
        import { z } from "zod";

        export const b = z.string();

        export const a = z.number();
    """)
    assert zod_reorder.reorder(source) == source


def test_property_key_matching_a_name_is_not_a_false_edge() -> None:
    # `colour:` is a property key, not a reference; only the value `colour`
    # creates the widget -> colour edge. A key alone must not (it could forge a
    # cycle). Here `name` is both a declared schema and a property key in widget.
    source = textwrap.dedent("""\
        import { z } from "zod";

        export const widget = z.object({
          name: z.string(),
        });

        export const name = z.string();
    """)
    # widget's only mention of `name` is the key, so no edge: order is preserved.
    assert zod_reorder.reorder(source) == source


def test_string_value_matching_a_name_is_not_a_false_edge() -> None:
    # `status` holds the enum value "widget", which equals the declared schema
    # `widget`. That string is not a reference; the only real edge is widget ->
    # status (the `state: status` value). Without stripping literals the phantom
    # status -> widget edge closes a cycle and reorder would raise spuriously.
    source = textwrap.dedent("""\
        import { z } from "zod";

        export const widget = z.object({
          state: status,
        });

        export const status = z.enum(["new", "widget", "done"]);
    """)
    expected = textwrap.dedent("""\
        import { z } from "zod";

        export const status = z.enum(["new", "widget", "done"]);

        export const widget = z.object({
          state: status,
        });
    """)
    assert zod_reorder.reorder(source) == expected


def test_comment_matching_a_name_is_not_a_false_edge() -> None:
    # The `// see widget` comment falls in `status`'s span (it precedes `widget`),
    # so an unstripped scan would read `widget` there and forge status -> widget;
    # with the real widget -> status edge that closes a spurious cycle. Comments
    # aren't references, so reorder must succeed and place status before widget.
    source = textwrap.dedent("""\
        import { z } from "zod";

        export const status = z.enum(["new", "done"]);

        // see widget for usage
        export const widget = z.object({
          state: status,
        });
    """)
    result = zod_reorder.reorder(source)
    assert result.index('export const status') < result.index('export const widget')


def test_raises_on_cycle() -> None:
    source = textwrap.dedent("""\
        import { z } from "zod";

        export const a = z.object({ b: b });

        export const b = z.object({ a: a });
    """)
    with pytest.raises(ValueError, match='cyclic schema reference'):
        zod_reorder.reorder(source)


def test_raises_when_no_declarations() -> None:
    with pytest.raises(ValueError, match='no `export const`'):
        zod_reorder.reorder('import { z } from "zod";\n')
