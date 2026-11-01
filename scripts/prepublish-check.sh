#!/usr/bin/env bash
# Pre-publish gate: fails while anything that must be settled before the public
# repository goes up is still open. Every problem is listed, not just the first.
#
# Usage: scripts/prepublish-check.sh [DIR]
#   DIR  the tree to check (default: this repository). Point it at the output of
#        scripts/export-public.sh to check exactly what will be pushed.

set -uo pipefail

dir=${1:-$(cd "$(dirname "$0")/.." && pwd)}
problems=0
problem() {
  echo "PREPUBLISH: $*" >&2
  problems=$((problems + 1))
}

manifest="$dir/PUBLISH-MANIFEST.txt"
if [ ! -f "$manifest" ]; then
  echo "PREPUBLISH FAIL: $manifest not found" >&2
  exit 1
fi
files=$(grep -v '^#' "$manifest" | sed '/^[[:space:]]*$/d' | LC_ALL=C sort -u)

# 1. Every listed file exists.
while IFS= read -r file; do
  [ -f "$dir/$file" ] || problem "listed in PUBLISH-MANIFEST.txt but missing: $file"
done <<< "$files"

# 2. Placeholders that only the maintainer can fill in.
for token in 'github.com/OWNER/' 'LIVE_SECONDS' 'DATA_POST_URL'; do
  if grep -q -F -- "$token" "$dir/README.md"; then
    problem "README.md still contains the placeholder $token"
  fi
done

# 3. The quickstart's live path must have a live run on record.
if grep -q -F 'live test pending' "$dir/README.md" "$dir/TESTING.md"; then
  problem "claude-cli is still marked 'live test pending'; run scripts/fresh-clone-test.sh <export> live and update README.md and TESTING.md"
fi
if grep -q -E '^- (Date|FRESH-CLONE line|Served model IDs[^:]*):[[:space:]]*$' "$dir/TESTING.md"; then
  problem "TESTING.md live run record has empty fields"
fi

# 4. House style: no em dashes in anything published.
while IFS= read -r file; do
  if [ -f "$dir/$file" ] && grep -q $'\xe2\x80\x94' "$dir/$file"; then
    problem "em dash (U+2014) in $file"
  fi
done <<< "$files"

# 5. When DIR is a git repository with commits, it must hold exactly the manifest.
if git -C "$dir" rev-parse --verify --quiet HEAD > /dev/null 2>&1; then
  tracked=$(git -C "$dir" ls-files | LC_ALL=C sort -u)
  if [ "$tracked" != "$files" ]; then
    problem "git ls-files differs from PUBLISH-MANIFEST.txt (export with scripts/export-public.sh to a new directory)"
  fi
  if [ "$(git -C "$dir" rev-list --count HEAD)" != "1" ]; then
    problem "history has more than one commit; publish a fresh export, never this repository's history"
  fi
fi

if [ "$problems" -gt 0 ]; then
  echo "PREPUBLISH FAIL: $problems problem(s)" >&2
  exit 1
fi
echo "PREPUBLISH OK"
