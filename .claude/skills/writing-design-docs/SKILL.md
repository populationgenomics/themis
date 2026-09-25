---
name: writing-design-docs
description: Write or substantially rewrite a design doc under `docs/design/`. The procedure: what to read before drafting, what stays in the doc versus what moves to the code, the shape to draft in, the self-read, and the checks. Use when authoring a new design doc, rewriting an existing one, splitting or folding one, or bringing a doc up to the guide.
---

# Writing a design doc

The guidance is `docs/style/design-docs.md` — the reader, the style, where low-level detail goes, the default shape, the
policy. This is the procedure; it does not restate the guide.

## Read first

- The guide.
- What the doc's reader has already read: `docs/PRODUCT.md`, `GLOSSARY.md`.
- The doc as it stands, where one exists, and the Overview of each doc that will appear under `Related`. Most docs
  predate the guide: take what they decide, not how they are written. The register to match is the exemplar the guide
  names.
- The code and contract files the doc describes — the proto, the module entry points, the tests. "The code states it" is
  verified there before a fact is left out, never assumed.

## Separate what stays from what restates code

Stays: the decisions, the named interfaces and what each promises, the consequences, the alternatives and why each was
rejected, open questions.

Goes: per-field paraphrase of a proto or schema, env-var names, paths beyond an entry point, function, class and test
names, error strings, constants.

For each code-owned fact the doc does not carry, note where it lives — the comment on the proto field, the docstring,
the test — or that it has no home yet because that code does not exist on this branch. Keep the list; it is part of the
report. No such fact goes into an inline comment beside the implementation (`docs/style/general.md`, Comments).

## Draft

1. For each decision, note the question it answers, the obvious answer and what that gets right, the fact that breaks
   it, one concrete instance of that fact (toy values where a real case carries noise), and what the decision costs.
   Where a decision has no obvious rival, note that; don't invent one. This list is the Design section's skeleton and
   the Overview's argument.
1. Choose the figures before writing prose: a mockup for each surface, a diagram with example values for each flow, a
   picture of each shape the reader must hold.
1. Write in the guide's default shape. Background teaches each term where the argument first needs it; no definition
   block.

## Re-read as the reader

Read the draft as the maintainer the guide describes — has read `PRODUCT.md` and `GLOSSARY.md`, knows nothing about the
area, one read on GitHub — and list the passages each question finds. Look again once when a list is empty; empty on the
second look is a pass.

- Which decisions could they not restate with the reason?
- Which sentences would they read twice? Split them, write out compressed noun phrases, drop the second aside.
- Which claims the argument rests on have no instance beside them?
- Which decisions are argued without the obvious alternative the reader would arrive holding?
- Where do the guide's machine-register patterns recur?

Fix the passage, not the reader.

## Checks

- `python3 tools/check_links.py` from the repo root — it scans all tracked Markdown, so read only the lines naming the
  doc.
- `pre-commit run mdformat --files <doc>`.
- After the push, check a mermaid block in the branch's file view on GitHub; the PR diff will not render it, so a
  reviewer opens the file.
- The header line carries `**Related:**`.

## Report

Where each code-owned fact lives, calling out the ones with no home yet so the author can push them onto the code's
documentation surface when it lands. Any question the writing raised for the author.
