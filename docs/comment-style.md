# Comment style

A comment is the last resort. Work down this ladder, and write prose only
when all four rungs fail. This applies to the Python in `vinowhisper/` and
the Rust in `tray/`, `gui/` and `settings/`.

1. **Extract it.** Logic that a comment explains belongs in a function with
   a domain name. Arithmetic spelled out in a comment belongs in named
   constants. A coercion that is explained twice belongs in one named helper.

   ```python
   # Before
   if len(samples) < 16000 * 0.3:  # 300 ms at 16 kHz

   # After
   if len(samples) < MIN_SPEECH_SAMPLES:
   ```

2. **Name it.** A comment that labels a block (`// 2. read the bundle`)
   means the block wants to be a function. Sentinels get names too: write
   `USE_APP_DEFAULT`, not `None  # = the app's default`.

3. **Pin it with a test pair.** A comment that states a business rule is a
   rule nothing enforces. Write one test inside the boundary and one
   outside, named after the rule, instead of writing `# exactly at the
   limit is fine`. The pair is the point: it fails the day someone moves
   the limit. In a test, a comment that labels the next few asserts is the
   same smell. Split those asserts into their own test, named after what
   the comment said.

4. **Pin cross-boundary rules with an end-to-end test.** The CLI contract,
   the server's HTTP API, the tray/server socket protocol, what the settings
   files round-trip to and what `install.sh` writes belong in a test that
   crosses that boundary, not in a paragraph.

5. **Only then, prose.** Hidden constraints, upstream bugs, protocol
   requirements, incident history that still constrains the code. State the
   constraint, not its biography: "spawn the child from the main thread:
   `PR_SET_PDEATHSIG` fires when the spawning thread exits" is enough
   without the story of how we found out. Measured claims carry a date. Longer
   rationale goes in the matching `docs/` page.

## Linking the test from the code

When rung 3 or 4 replaced a comment, the code it governs says which test
pins the rule, always with the same phrase:

```rust
// Under the limit is kept; at it is dropped (pinned by
// `tests::keeps_a_session_just_under_the_limit`).
```

```python
# (pinned by `test_stitch.py::test_a_repeat_at_the_seam_is_dropped`)
```

Rust cites `` `tests::name` `` (the file's own test module, or its
`<file>/tests.rs`) or `` `module::tests::name` ``, resolved inside the same
crate. Python cites `` `test_file.py::test_name` `` under `tests/`.
rustdoc can't link to `#[test]` functions, so these are code spans, not
links. Two checks keep them honest, both part of the "Doc Links" CI job:
`settings/tests/doc_links.rs` for the three crates and
`tests/test_doc_links.py` for the Python. Each fails on a citation with no
such test.

## Exempt

- `///` and `//!` docs on items, which say what an item is and how to call
  it. They follow the same "constraint, not biography" rule, and a doc that
  restates the signature goes.
- `characterization:` docstrings on `test_characterization_*` tests (see
  `CLAUDE.md`).
- `// SAFETY:` comments on `unsafe`, `#[allow]` and `# noqa` / `# type:
  ignore` reasons, and coverage pragmas.
