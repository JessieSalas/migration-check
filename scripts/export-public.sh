#!/usr/bin/env bash
# Build the public repository with fresh history from PUBLISH-MANIFEST.txt only.
#
# Usage: scripts/export-public.sh DEST [--commit]
#   DEST      a new or empty directory. Exactly the files listed in
#             PUBLISH-MANIFEST.txt are copied into it, `git init` runs, and the
#             files are staged by name. Nothing from this repository's history,
#             its internal notes or its logs is carried over.
#   --commit  also make the single first commit. Without it the script prints
#             the commit command for you to run after a last look.
#
# Then test the export as a stranger would, through a real clone:
#   scripts/fresh-clone-test.sh file://DEST offline
#   scripts/fresh-clone-test.sh file://DEST live
# and run scripts/prepublish-check.sh DEST before pushing it anywhere.

set -euo pipefail

if [ "$#" -lt 1 ] || [ "$#" -gt 2 ] || { [ "$#" -eq 2 ] && [ "$2" != "--commit" ]; }; then
  echo "usage: $0 DEST [--commit]" >&2
  exit 2
fi
root=$(cd "$(dirname "$0")/.." && pwd)
dest=$1

if [ -e "$dest" ] && [ -n "$(ls -A "$dest")" ]; then
  echo "export-public: $dest is not empty; choose a new directory" >&2
  exit 1
fi
mkdir -p "$dest"
dest=$(cd "$dest" && pwd)

list=$(mktemp)
trap 'rm -f "$list"' EXIT
grep -v '^#' "$root/PUBLISH-MANIFEST.txt" | sed '/^[[:space:]]*$/d' | LC_ALL=C sort -u > "$list"
while IFS= read -r file; do
  if [ ! -f "$root/$file" ]; then
    echo "export-public: $file is listed in PUBLISH-MANIFEST.txt but missing" >&2
    exit 1
  fi
done < "$list"

tar -C "$root" -cf - -T "$list" | tar -C "$dest" -xf -
git -C "$dest" init --quiet
while IFS= read -r file; do
  git -C "$dest" add -- "$file"
done < "$list"
echo "export-public: staged $(git -C "$dest" diff --cached --name-only | wc -l | tr -d ' ') files in $dest"

version=$(sed -n 's/^version = "\(.*\)"$/\1/p' "$root/pyproject.toml")
if [ "${2:-}" = "--commit" ]; then
  git -C "$dest" commit --quiet -m "migration-check v$version"
  echo "export-public: committed migration-check v$version"
else
  echo "export-public: review it, then run: git -C '$dest' commit -m 'migration-check v$version'"
fi
