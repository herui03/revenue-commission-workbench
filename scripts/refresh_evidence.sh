#!/usr/bin/env bash
# Regenerate every evidence artifact in docs/evidence/ and the offline replay page.
# Uses the project venv for Flask/Playwright steps; the stdlib steps run with plain python3.
set -euo pipefail
cd "$(dirname "$0")/.."
PY="${PY:-.venv/bin/python}"
EV=docs/evidence
mkdir -p "$EV"
TMP="$(mktemp -d)"
trap 'rm -rf "$TMP"' EXIT

echo "1/6 full test suite (pytest, includes web tests)"
"$PY" -m pytest -v -p no:cacheprovider > "$EV/test_results.txt" 2>&1 || { tail -20 "$EV/test_results.txt"; exit 1; }
tail -1 "$EV/test_results.txt"

echo "2/6 stdlib-only run (no Flask: web tests are reported as skipped)"
python3 -m unittest discover -s tests -t . > "$EV/unittest_stdlib_only.txt" 2>&1 || { tail -20 "$EV/unittest_stdlib_only.txt"; exit 1; }
tail -3 "$EV/unittest_stdlib_only.txt"

echo "3/6 scripted demo story"
rm -rf "$EV/demo_exports"
python3 -m rcw --db "$TMP/demo.db" demo --out "$EV/demo_exports" > "$EV/demo_story_output.txt" 2>&1
grep "byte-identical" "$EV/demo_story_output.txt"

echo "4/6 10k benchmark"
python3 scripts/benchmark.py > "$EV/benchmark_run.txt" 2>&1
head -3 "$EV/benchmark_run.txt"

echo "5/6 browser end-to-end + screenshots"
"$PY" scripts/e2e_browser.py > "$EV/e2e_run.txt" 2>&1 || { tail -30 "$EV/e2e_run.txt"; exit 1; }
tail -1 "$EV/e2e_run.txt"

echo "6/6 offline replay page"
python3 scripts/build_presentation.py
