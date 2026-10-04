# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

`cc-hwp` (the directory name is `claude-code-hwp-kit`): a skill, meant to ship as a Claude Code plugin and as a standalone claude.ai Skill, that lets agents read Korean Hangul documents (`.hwp` HWP 5.0 binary, `.hwpx` OWPML ZIP) as Markdown.
Read-only for now; the goal includes editing, and an `.mcp.json` server was suggested as the path for it.

## Commands

Run from the repository root.

```bash
# Full test suite (stdlib unittest, no install step)
python3 -m unittest discover tests

# One test (name substring match)
python3 -m unittest discover tests -k test_hwpx

# Exercise the script directly
python3 skills/hwp-read/scripts/hwp_read.py extract tests/fixtures/e-phi-design.hwp -o out.md   # JSON report on stderr
python3 skills/hwp-read/scripts/hwp_read.py info tests/fixtures/sk-openinno-form.hwpx
python3 skills/hwp-read/scripts/hwp_read.py render tests/fixtures/e-phi-design.hwp -o out.pdf
```

`tests/` has no `__init__.py`, so `discover -s tests -t .` fails with "Start directory is not importable"; use the form above.
No linter or formatter is configured yet.

## Hard constraints

- **Stdlib only, Python 3.8+.** `skills/hwp-read/` must stay self-contained so the folder can be zipped and uploaded to claude.ai as a Skill with no `pip install`. That is why the CFB (OLE2) container reader is hand-written instead of using `olefile`.
- **Format is detected by magic bytes, never by extension.** Files named `.hwp` that are really HWPX ZIPs (and the reverse) exist in the wild.
- **Never return an empty result silently.** Protected (배포용/암호), HWP 3.x and HML inputs raise `Unsupported` and exit `3` with a message that says what to do instead.

## Architecture

Everything lives in `skills/hwp-read/scripts/hwp_read.py`; the pipeline is:

```plaintext
detect(bytes) ─┬─ hwp5 → CFB → Hwp5Reader ─┐
               └─ hwpx → zipfile+ET → HwpxReader ─┴→ Doc IR ─┬→ to_markdown() → stats() / preview_coverage()
                                                             └→ to_docx()  (convert --to docx)
```

- **IR** (`Doc`, `Para`, `Table`, `Cell`) is the seam between the two readers and the renderer. A new input format only needs a reader that produces this IR.
- **HWP 5.0 reader:** `BodyText/SectionN` streams are raw-deflate compressed when FileHeader flag bit 0 is set. Records are flattened and rebuilt into a tree by their `level` field (`_tree`). Inside `PARA_TEXT`, extended control characters (`EXTENDED_CTRL`) occupy 8 UTF-16 units and map **in order** to the paragraph's `CTRL_HEADER` children: `_para` keeps a cursor `ci` across them, so skipping or reordering control handling desynchronizes every later table or footnote in that paragraph.
- **HWPX reader:** section order comes from the `content.hpf` spine, falling back to sorted `Contents/sectionN.xml`. Drawing objects with `hp:subList` children are treated as text boxes.
- **DOCX** is written from the IR, not from the Markdown: Markdown flattens layout boxes, so going through it loses tables. Every `Table` becomes a `w:tbl`; merges map to `gridSpan` and `vMerge`, and a vertical-merge continuation cell repeats its start cell's `gridSpan`.
- **Table rendering** has three outcomes, chosen in `_table_md`: one-row/one-column tables without nesting are layout boxes and are flattened to text; simple grids become pipe tables; any merged or nested table becomes HTML with `rowspan`/`colspan`.
- **Self-check:** `preview_coverage` compares the output against `PrvText`, the authoring app's own preview of roughly the first 1–2K characters. Below `0.9`, `extract` still writes output but exits `4`.
- **`render`:** delegates to the `rhwp` CLI (github.com/edwardkim/rhwp, the engine behind the HOP viewer) when it is on PATH; otherwise it writes the embedded first-page thumbnail. Verified against rhwp v0.8.6 on 2026-10-03: `export-pdf <file> -o <file.pdf> --json -p N` renders and prints a JSON manifest. rhwp also ships editing commands (`rhwp edit replace-text`, `set-cell`, `fill-fields`, …); run `rhwp --help` for the current surface rather than relying on a list here.

## Contracts that span files

- **Exit codes** are defined in the script docstring and repeated in `SKILL.md` and `README.md`. Change all three together.
- **`SKILL.md` is the agent-facing spec of the output:** it documents every report field and output convention. Any change to `to_markdown()` or `stats()` needs a matching `SKILL.md` edit.
- User-facing messages (`Unsupported` texts, warnings, `README.md`) are intentionally Korean; keep them Korean.

## Tests

- Each fixture test asserts `preview_coverage == 1.0`, a minimum table count, and **character conservation**: every visible character in the raw source text records must survive into the Markdown. The raw text is re-extracted by test-local code (`_raw_hwp5_chars`, `_raw_hwpx_chars`) that bypasses tree, paragraph and table assembly. The HWP 5.0 side still reuses `CFB`, `_records`, and the `EXTENDED_CTRL`/`INLINE_CTRL` tables, so a bug in those layers corrupts both sides of the comparison and passes; the conservation test does not protect edits there.
- `test_distribution_flag_rejected` flips the 배포용 bit in an in-memory copy of a real fixture's FileHeader rather than shipping a protected sample.
- Fixtures are real public documents on purpose; generator-made samples are too clean to find parser limits. Still missing: documents with footnotes, equations, pictures inside tables, and a genuine 배포용 file. Add those as regression fixtures when available.
- The two current fixtures (a 한국동서발전 service design document and a 서울창업센터 application form) are local-only by the operator's decision (2026-10-03): they are publicly posted documents but are not committed.
