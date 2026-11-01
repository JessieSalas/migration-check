#!/usr/bin/env bash
# Fresh-clone test: what a stranger does, timed, in a new temporary directory.
#
# Usage: scripts/fresh-clone-test.sh SOURCE MODE [INSTALL]
#   SOURCE   a git URL (https://..., or file:///path for a local repository with
#            commits), or a local directory.
#            A git URL is cloned with `git clone`, which is what a stranger does,
#            and the clone's `git ls-files` must equal its PUBLISH-MANIFEST.txt.
#            A local directory is copied, not cloned: exactly the files listed in
#            its PUBLISH-MANIFEST.txt. The script fails if a listed file is missing,
#            or if the working tree holds any other publishable file (tracked, or
#            untracked and not ignored, minus tracked files that are ignored).
#   MODE     offline  runs `migration-check sample --offline` (mock providers)
#            live     runs `migration-check sample` (calls the claude CLI)
#   INSTALL  local    (default) pip install with no network: --no-index --no-deps
#                     --no-build-isolation, in a venv created with
#                     --system-site-packages so it can reuse the system setuptools
#                     (and wheel, which setuptools older than 70.1 needs).
#            network  pip install with normal build isolation, as pipx and pip do
#                     for a stranger: the current setuptools comes from PyPI, so
#                     this needs network access.
#
# The unit tests run after the sample, so clone_to_report measures what a new
# user waits for, not the test suite. The temporary directory is kept and printed.

set -euo pipefail

fail() {
  echo "FRESH-CLONE FAIL: $*" >&2
  exit 1
}

if [ "$#" -lt 2 ] || [ "$#" -gt 3 ] || { [ "$2" != "offline" ] && [ "$2" != "live" ]; } ||
  { [ "$#" -eq 3 ] && [ "$3" != "local" ] && [ "$3" != "network" ]; }; then
  echo "usage: $0 SOURCE offline|live [local|network]" >&2
  exit 2
fi
source_path=$1
mode=$2
install=${3:-local}
python=${PYTHON:-python3}

now() { "$python" -c 'import time; print(f"{time.time():.3f}")'; }
since() { "$python" -c "import sys; print(f'{float(sys.argv[2]) - float(sys.argv[1]):.1f}')" "$1" "$2"; }
manifest_list() { grep -v '^#' "$1" | sed '/^[[:space:]]*$/d' | LC_ALL=C sort -u; }

work=$(mktemp -d "${TMPDIR:-/tmp}/migration-check-fresh.XXXXXX")
repo="$work/migration-check"
echo "fresh-clone: working in $work"

clone_start=$(now)
if [ -d "$source_path" ]; then
  [ -f "$source_path/PUBLISH-MANIFEST.txt" ] || fail "$source_path has no PUBLISH-MANIFEST.txt"
  manifest_list "$source_path/PUBLISH-MANIFEST.txt" > "$work/manifest.txt"
  git -C "$source_path" ls-files -co --exclude-standard | LC_ALL=C sort -u > "$work/candidates.txt"
  git -C "$source_path" ls-files -ci --exclude-standard | LC_ALL=C sort -u > "$work/ignored.txt"
  # Tracked files deleted from the working tree are not publishable either.
  LC_ALL=C comm -23 "$work/candidates.txt" "$work/ignored.txt" | while IFS= read -r file; do
    if [ -f "$source_path/$file" ]; then printf '%s\n' "$file"; fi
  done > "$work/publishable.txt"
  if ! LC_ALL=C diff "$work/manifest.txt" "$work/publishable.txt" > "$work/manifest.diff"; then
    echo "fresh-clone: '<' listed in PUBLISH-MANIFEST.txt but missing, '>' publishable but not listed:" >&2
    cat "$work/manifest.diff" >&2
    fail "working tree does not match PUBLISH-MANIFEST.txt"
  fi
  mkdir -p "$repo"
  tar -C "$source_path" -cf - -T "$work/manifest.txt" | tar -C "$repo" -xf -
  echo "fresh-clone: copied the $(wc -l < "$work/manifest.txt" | tr -d ' ') files in PUBLISH-MANIFEST.txt"
else
  git clone --quiet "$source_path" "$repo"
  [ -f "$repo/PUBLISH-MANIFEST.txt" ] || fail "the clone has no PUBLISH-MANIFEST.txt"
  manifest_list "$repo/PUBLISH-MANIFEST.txt" > "$work/manifest.txt"
  git -C "$repo" ls-files | LC_ALL=C sort -u > "$work/cloned.txt"
  if ! LC_ALL=C diff "$work/manifest.txt" "$work/cloned.txt" > "$work/manifest.diff"; then
    echo "fresh-clone: '<' listed in PUBLISH-MANIFEST.txt but not in the clone, '>' in the clone but not listed:" >&2
    cat "$work/manifest.diff" >&2
    fail "the cloned repository does not match its PUBLISH-MANIFEST.txt"
  fi
  echo "fresh-clone: cloned $(wc -l < "$work/cloned.txt" | tr -d ' ') files, matching PUBLISH-MANIFEST.txt"
fi

if [ "$install" = "network" ]; then
  "$python" -m venv "$work/venv"
  venv_python="$work/venv/bin/python"
  (cd "$repo" && "$venv_python" -m pip install --quiet .) || fail "pip install (network, build isolation) failed"
else
  "$python" -m venv --system-site-packages "$work/venv"
  venv_python="$work/venv/bin/python"
  (cd "$repo" && "$venv_python" -m pip install --quiet --no-build-isolation --no-deps --no-index .) ||
    fail "pip install (no network) failed"
fi

# Run from a directory outside the source tree, so the installed package (and its
# bundled sample data) is what runs, not the checkout.
mkdir -p "$work/user"
(cd "$work/user" && "$venv_python" - "$repo" <<'PY') || fail "installed package check failed"
import importlib.metadata as metadata
import pathlib
import sys

import migration_check

version = metadata.version("migration-check")  # raises if the distribution name is wrong (e.g. UNKNOWN)
if version != migration_check.__version__:
    sys.exit(f"installed distribution version {version} != package version {migration_check.__version__}")
package = pathlib.Path(migration_check.__file__).resolve().parent
if pathlib.Path(sys.argv[1]).resolve() in package.parents:
    sys.exit(f"migration_check was imported from the checkout ({package}), not the install")
for name in ("tickets.jsonl", "labels.txt", "README.md"):
    if not (package / "sample" / name).is_file():
        sys.exit(f"bundled sample file missing from the install: {package / 'sample' / name}")
if not (package / "report.html").is_file():
    sys.exit("report.html missing from the install")
print(f"fresh-clone: installed migration-check {version} at {package}")
PY

projects="$HOME/.claude/projects"
count_transcripts() {
  if [ -d "$projects" ]; then
    find "$projects" -maxdepth 1 -type d -name '*migration-check-*' | wc -l | tr -d ' '
  else
    echo 0
  fi
}
transcripts_before=$(count_transcripts)

out="$work/user/migration-check-sample"
sample_args=(sample --out "$out")
if [ "$mode" = "offline" ]; then
  sample_args+=(--offline)
fi
sample_start=$(now)
if ! (cd "$work/user" && "$work/venv/bin/migration-check" "${sample_args[@]}") | tee "$work/sample.out"; then
  fail "migration-check sample failed (output above)"
fi
report_written=$(now)

for name in report.html report.decision.json report.decision.md report.analysis.json; do
  [ -s "$out/$name" ] || fail "$out/$name is missing or empty"
done
if [ "$mode" = "offline" ] && ! grep -q '"simulated": true' "$out/report.analysis.json"; then
  fail "offline report is not labelled simulated"
fi
if [ "$mode" = "live" ]; then
  if grep -q '"simulated": true' "$out/report.analysis.json"; then
    fail "live report is labelled simulated"
  fi
  "$venv_python" - "$out/report.analysis.json" <<'PY' || fail "live report shows adapter problems (above)"
import json
import sys

data = json.load(open(sys.argv[1], encoding="utf-8"))
problems = []
for arm in ("old", "new"):
    if not data["served_models"].get(arm):
        problems.append(f"no served model ID recorded for {arm}")
markers = ("provider HTTP", "provider unreachable", "provider response exceeds", "claude-cli error",
           "claude-cli invalid envelope", "every collected call failed", "attempts have no served-model ID")
for warning in data["warnings"]:
    if any(marker in warning for marker in markers):
        problems.append("adapter warning: " + warning)
print("fresh-clone: served models " + json.dumps(data["served_models"]))
for problem in problems:
    print("fresh-clone: " + problem, file=sys.stderr)
sys.exit(1 if problems else 0)
PY
  transcripts_after=$(count_transcripts)
  if [ "$transcripts_after" -gt "$transcripts_before" ]; then
    fail "new ~/.claude/projects/*migration-check-* directories appeared ($transcripts_before -> $transcripts_after); session persistence is not off"
  fi
  echo "fresh-clone: no new Claude Code transcript directories ($transcripts_after before and after)"
fi

if ! (cd "$repo" && "$venv_python" -m unittest discover -s tests -q) > "$work/tests.out" 2>&1; then
  cat "$work/tests.out" >&2
  fail "unit tests failed"
fi
tests=$(sed -n 's/^Ran \([0-9][0-9]*\) tests\{0,1\} in .*/\1/p' "$work/tests.out")
[ -n "$tests" ] || fail "test count not found in the unittest output"
tail -n 1 "$work/tests.out"

echo "fresh-clone: report at $out/report.html"
echo "fresh-clone: temp dir kept at $work"
echo "FRESH-CLONE OK mode=$mode install=$install clone_to_report=$(since "$clone_start" "$report_written")s sample_to_report=$(since "$sample_start" "$report_written")s tests=$tests"
