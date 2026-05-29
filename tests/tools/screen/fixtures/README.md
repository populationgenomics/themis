# Screen fixtures

Each `*.diff` file in this directory is a real `git diff --unified=0`
output captured against a synthetic repo. The tests in `../test_regex_screen.py`
feed them to `_iter_added_lines` and assert on the parsed result.

## Identifier convention

Fixtures must not contain strings shaped like CPG / XPG identifiers
(matching `\b[CX]PG\d+\b`). Use the `TST` prefix instead — e.g.
`TST123`, `TST456`.

Two reasons:

1. **Public mirror.** The fixture content is published verbatim to
   `populationgenomics/themis`. A line like `PARTICIPANT = "CPG12345"` <!-- screen-ignore: example illustrating the convention -->
   is indistinguishable from a real ID to a casual reader, even when
   it's tagged with a `screen-ignore` marker.

2. **Avoid self-screening churn.** A fixture containing `CPG\d+` would
   trip the regex screen on the PR that introduces it, requiring a
   `screen-ignore` marker that itself becomes part of the test data.
   The `TST` prefix sidesteps the problem entirely.

## End-to-end pattern-matcher tests

If a test genuinely needs to exercise the pattern matcher against
identifier-shaped content, monkeypatch the pattern list rather than
seeding a fixture with real-shaped IDs:

```python
def test_end_to_end(tmp_path, monkeypatch):
    patterns = tmp_path / 'patterns.yaml'
    patterns.write_text('- name: tst\n  regex: \\bTST\\d+\\b\n')
    # ...drive the script with this pattern file instead of the real one.
```

The fixtures themselves stay TST-only.
