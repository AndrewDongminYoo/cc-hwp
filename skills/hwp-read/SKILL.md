---
name: hwp-read
description: Read Korean Hangul (한글/한컴오피스) documents — .hwp (HWP 5.0) and .hwpx (OWPML) — as Markdown with tables, text boxes and footnotes preserved, and render pages for visual checks. Use whenever the user provides or mentions an .hwp/.hwpx file, a 한글 문서, 공고문, 신청서 양식, or asks to summarize, search, compare or fill in information from one. No Hancom Office, LibreOffice or PDF conversion needed.
---

# hwp-read

Reads HWP/HWPX directly. The bundled script is pure Python 3.8+ stdlib (no pip install),
so it works in any sandbox. All paths below are relative to this SKILL.md's directory.

## Workflow

1. **Extract** — always start here:
   ```bash
   python3 scripts/hwp_read.py extract "<file>" -o /tmp/doc.md
   ```
   Markdown goes to `-o` (or stdout); a one-line JSON report goes to stderr. Then Read `/tmp/doc.md`.
   For very long documents, Read the file in ranges instead of loading it all at once,
   or pass `--max-chars N` (truncation is marked explicitly, never silent).

2. **Check the report before answering.** Fields that matter:
   - `preview_coverage` — share of the authoring app's own preview text (`PrvText`, ~first 1–2K chars)
     found in the extraction. `1.0` is expected. Below `0.9` → exit code 4: content may be missing;
     cross-check with step 4 and tell the user what you could not verify.
   - `tables` / `merged_tables` — merged-cell tables are emitted as HTML `<table>` with
     `rowspan`/`colspan`; read them as grids, don't flatten mentally.
   - `equations`, `pictures` — equations appear as `[수식: <Hancom script>]` (not LaTeX);
     pictures as `[그림]`. Neither is interpreted. Say so if the answer depends on them.
   - `embedded_files` counts BinData entries; these can be background/border images rather
     than content. Use `--images-dir DIR` to dump them and view with Read only if relevant.
   - `warnings` — surface every warning to the user in one line.

3. **Exit codes**: `0` ok · `1` parse error (corrupt file) · `2` usage · `3` unsupported or
   protected (배포용/암호 문서, HWP 3.x, HML, or a container refused as unsafe: no body section,
   a DTD, or a part that inflates past the script's size cap) — relay the message, which says what to do ·
   `4` extracted but suspicious (see `preview_coverage`).

4. **Render when layout matters** (filled-in forms, checkbox states, signature/seal areas,
   page-specific questions, or a low coverage score):
   ```bash
   python3 scripts/hwp_read.py render "<file>" -o /tmp/doc.pdf [--page N]
   ```
   - If the `rhwp` CLI is on PATH, this produces a layout-faithful PDF (all pages or page N, 0-based).
   - Otherwise it writes the document's embedded **first-page thumbnail** (`*_preview.png`) and
     says so in its JSON. View it with Read. Don't claim you checked later pages visually.

## Reading conventions in the output

- One-row or one-column tables without nesting are layout boxes (title banners, chapter headers,
  notice boxes) and are flattened to text; one-row boxes join cells with ` | `.
- Text-box content (drawing objects) is emitted as `> ` blockquotes at its anchor position.
- Footnotes/endnotes become `[^n]` markers with definitions at the end.
- Headers/footers and page numbers are dropped.
- Korean forms often contain empty answer cells (`<td></td>`) — these are blanks to be filled,
  not extraction failures. `□`/`■` are unchecked/checked boxes as typed in the source.

## When the `rhwp` CLI is available

`rhwp` (github.com/edwardkim/rhwp, the engine behind the HOP viewer) also handles formats this
script refuses: `rhwp export-markdown`, `rhwp export-text --json --max-chars N`,
`rhwp search <file> -- <term>` (page-addressed hits), and `rhwp convert` for read-only
distribution documents. Prefer it for those cases; keep using `hwp_read.py extract` as the
default because its output and report format are what this skill documents.

## Don't

- Don't convert to PDF first just to read text — extraction is lossless for text and keeps table structure.
- Don't write back to `.hwp`/`.hwpx`; this skill is read-only.
