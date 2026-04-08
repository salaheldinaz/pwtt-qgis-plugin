#!/usr/bin/env bash
# Auto-bump version in metadata.txt based on conventional commits.
#
# Commit prefixes:
#   fix:      -> patch bump  (0.1.0 -> 0.1.1)
#   feat:     -> minor bump  (0.1.0 -> 0.2.0)
#   BREAKING: -> major bump  (0.1.0 -> 1.0.0)
#
# Usage:
#   ./scripts/bump-version.sh           # auto-detect from last commit message
#   ./scripts/bump-version.sh patch     # force patch
#   ./scripts/bump-version.sh minor     # force minor
#   ./scripts/bump-version.sh major     # force major
set -euo pipefail
cd "$(git rev-parse --show-toplevel)"

METADATA="metadata.txt"

current=$(sed -n 's/^version=\(.*\)/\1/p' "$METADATA" | tr -d '[:space:]')
if [ -z "$current" ]; then
    echo "ERROR: Could not read version from $METADATA" >&2
    exit 1
fi

IFS='.' read -r major minor patch <<< "$current"

# Determine bump type
bump="${1:-auto}"
if [ "$bump" = "auto" ]; then
    msg=$(git log -1 --pretty=%B 2>/dev/null || echo "")
    if echo "$msg" | grep -qi '^BREAKING'; then
        bump="major"
    elif echo "$msg" | grep -qi '^feat'; then
        bump="minor"
    else
        bump="patch"
    fi
fi

case "$bump" in
    major) major=$((major + 1)); minor=0; patch=0 ;;
    minor) minor=$((minor + 1)); patch=0 ;;
    patch) patch=$((patch + 1)) ;;
    *)     echo "Usage: $0 [patch|minor|major|auto]" >&2; exit 1 ;;
esac

new_version="${major}.${minor}.${patch}"
echo "Bumping $current -> $new_version ($bump)"

# Update metadata.txt version
sed -i.bak "s/^version=.*/version=${new_version}/" "$METADATA"
rm -f "${METADATA}.bak"

# Prepend/update changelog key (QGIS metadata requires key=value format)
# Grab the last commit subject as the changelog line
last_subject=$(git log -1 --pretty=%s 2>/dev/null || echo "Release ${new_version}")
changelog_entry="${new_version} - ${last_subject} ($(date +%Y-%m-%d))"

if grep -q '^changelog=' "$METADATA"; then
    current_changelog=$(sed -n 's/^changelog=//p' "$METADATA")
    # Do not use sed here: changelog text can contain "|" (e.g. "|t|"), which breaks s|...|...|
    python3 -c '
import sys
path, entry, prev = sys.argv[1:4]
new_line = "changelog=" + entry + "; " + prev
out_lines = []
with open(path, encoding="utf-8") as f:
    for line in f:
        if line.startswith("changelog="):
            out_lines.append(new_line + "\n")
        else:
            out_lines.append(line)
with open(path, "w", encoding="utf-8") as f:
    f.writelines(out_lines)
' "$METADATA" "$changelog_entry" "$current_changelog"
else
    printf "\nchangelog=%s\n" "$changelog_entry" >> "$METADATA"
fi

echo "Updated $METADATA to version $new_version"
echo "$new_version"
