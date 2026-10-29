# Quality Tooling Refresh Plan

## Work

1. Verify repository identity, instructions, current changes and Oracle precedent → verify the absolute Git root, origin, branch and scoped source claims.
2. Add the minimal local Trunk baseline without application dependencies → verify resolved linter commands, Pinact locking and non-formatting actions.
3. Run the full unittest command and separate fixture-independent tests → record exact failures and case names without bypassing or replacing missing fixtures.
4. Prove offline secret detection with a temporary canary and run the full no-fix Trunk scan when the shared execution slot is available → require the expected canary diagnostic and a clean scan after removing it.
5. Reconcile affected knowledge and review the scoped candidate → verify source syntax in memory, plugin metadata, internal links and diff whitespace.
6. Keep the candidate uncommitted while the full test gate is blocked → retain visible evidence and the exact missing prerequisite for the root agent.

## Declared Gates

- `python3 -m unittest discover tests`
- `trunk check --all --no-fix --jobs=2`

## Evidence and Remaining Gate

Keep logs and the knowledge claim inventory in `/Volumes/dongminyu/Development/01_personal/pr-loop-reviews/cc-hwp/2026-10-28-quality-refresh`.
The full unittest command requires ignored local documents `tests/fixtures/e-phi-design.hwp` and `tests/fixtures/sk-openinno-form.hwpx`.
Both were absent at the initial fixture preflight.
The operator restored the exact local documents on 2026-10-29.
The unchanged full suite passed all 60 original tests after fixture recovery.
The reader suite now covers 17 cases, including bounded source context and cache eviction.
Re-run the full no-fix Trunk check after each scanner repair before pushing.
The missing-fixture block is resolved; local commits may proceed, while push and PR creation still require repository-specific approval.

## Offline scanner boundary

Run `python3 -B .trunk/configs/tests/test_trufflehog_to_sarif.py` to exercise the 17 filesystem JSONL diagnostic reader cases.
Trunk 1.25.0 consumes native paths in the SARIF path field; URI encoding drops findings for reserved filenames.
This adapter preserves native paths for Trunk and does not promise portable SARIF URI conformance.
Columns use Unicode code points, encoded findings retain file-level diagnostics, and scanner input errors fail the gate with `--fail-on-scan-errors`.
No candidate credential is verified with its provider.
Application source and document fixtures remain unchanged.

Source-location analysis reads at most 1 MiB plus one sentinel byte per file and caches at most eight files.
Oversized sources retain file-level findings without a region.
These limits bound source context; they do not bound all result memory or guarantee that filesystem I/O cannot block.
