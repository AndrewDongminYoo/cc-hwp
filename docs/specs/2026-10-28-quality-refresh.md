# Quality Tooling Refresh

## Approved Scope

Refresh supported dependencies and existing quality tooling.
Preserve public APIs, generated artifacts and repository-specific boundaries.
The uploadable Skill remains Python standard-library-only and supports Python 3.8+.
There is no dependency manifest or lockfile to refresh.
Do not add Python formatters or rewrite application, test, fixture, plugin metadata or media files.

## Local Quality Baseline

- Use the managed Trunk CLI `1.25.0` and plugin source `v1.11.0`; these are the selected baseline, not a claim about the latest CLI release.
- Enable `git-diff-check` and TruffleHog `3.99.7` with `--no-verification --no-update`.
- Pin Pinact to `4.1.1` with `lint` and `upgrade`.
- No GitHub Actions files are tracked, so Actionlint has no applicable target.
- Enable the pre-push check and leave automatic formatting disabled.

## Acceptance

- Pinact remains locked to `4.1.1` with `lint` and `upgrade`.
- The full no-fix Trunk scan and declared project gates pass.
- Changed manifests have package-manager-generated lockfiles under repository policy.
- Advisory dispositions have specific evidence and affected knowledge claims are reconciled.
- Prove secret detection through the configured Trunk diagnostic reader with a temporary fake credential, then remove that exact canary and recheck.
- The declared full unittest gate requires the authorized local copies of `tests/fixtures/e-phi-design.hwp` and `tests/fixtures/sk-openinno-form.hwpx`.
- Record a missing-fixture failure as blocked; fixture-independent test results are partial evidence only.

## Preserved Precedent

The indexed project is `claude-code-hwp-kit`, resolved from the repository identity rather than the checkout directory name.
The Oracle snapshot at source commit `9670d54de3076c5239713f54900c7a855bd18478` has unverified freshness and is partial evidence.
Its sources `wiki/sources/claude-code-hwp-kit--claude.md` and `raw/sources/claude-code-hwp-kit/CLAUDE.md` confirm the standard-library-only runtime, magic-byte detection, explicit failure behavior, resource budgets and local-only fixture boundary.
The current source and instructions confirm preserving those contracts during the tooling change.

## Publication

Push and PR creation require explicit repository-specific approval.
After approval, continue the requested hosted gates, merge, cleanup and memory recording.

## Offline scanner boundary

Run `python3 -B .trunk/configs/tests/test_trufflehog_to_sarif.py` to exercise the 15 filesystem JSONL diagnostic reader cases.
Trunk 1.25.0 consumes native paths in the SARIF path field; URI encoding drops findings for reserved filenames.
This adapter preserves native paths for Trunk and does not promise portable SARIF URI conformance.
Columns use Unicode code points, encoded findings retain file-level diagnostics, and scanner input errors fail the gate with `--fail-on-scan-errors`.
No candidate credential is verified with its provider.
Application source and document fixtures remain unchanged.
