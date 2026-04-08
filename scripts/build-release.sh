#!/usr/bin/env bash
# Build a QGIS-installable plugin ZIP under build/ (gitignored — publish via forge Releases, not git).
#
# Standalone repo layout: plugin files live at repo root. The ZIP contains a
# single top-level folder pwtt_qgis/ (QGIS python/plugins module name) with the
# same files — ready for Plugin Manager "Install from ZIP".
#
# Usage:
#   ./scripts/build-release.sh              # build from current metadata version
#   ./scripts/build-release.sh --bump       # bump version first, then build
#   ./scripts/build-release.sh --bump minor # bump minor, then build
set -euo pipefail
ROOT="$(git rev-parse --show-toplevel)"
cd "$ROOT"

PLUGIN_SLUG="pwtt_qgis"
METADATA="${ROOT}/metadata.txt"
STAGING="${ROOT}/build/${PLUGIN_SLUG}"

# Optional version bump
if [ "${1:-}" = "--bump" ]; then
    bump_arg="${2:-auto}"
    bash "${ROOT}/scripts/bump-version.sh" "$bump_arg"
fi

# Read version
version=$(sed -n 's/^version=\(.*\)/\1/p' "$METADATA" | tr -d '[:space:]')
if [ -z "$version" ]; then
    echo "ERROR: Could not read version from $METADATA" >&2
    exit 1
fi

# Recompile Qt resources (pyrcc5, pyrcc6, or uv)
if command -v pyrcc5 &>/dev/null; then
    echo "Compiling resources.qrc (pyrcc5)..."
    pyrcc5 -o "${ROOT}/resources_rc.py" "${ROOT}/resources/resources.qrc"
elif command -v pyrcc6 &>/dev/null; then
    echo "Compiling resources.qrc (pyrcc6)..."
    pyrcc6 -o "${ROOT}/resources_rc.py" "${ROOT}/resources/resources.qrc"
elif command -v uv &>/dev/null; then
    echo "Compiling resources.qrc via uv..."
    (cd "${ROOT}/resources" && uv run --with PyQt5 pyrcc5 -o ../resources_rc.py resources.qrc)
else
    echo "WARN: pyrcc5/pyrcc6 not found — using existing resources_rc.py"
fi

# Stage plugin tree (flat repo -> pwtt_qgis/ in zip)
rm -rf "${ROOT}/build"
mkdir -p "$STAGING"

rsync -a \
    --exclude='.git' \
    --exclude='.github' \
    --exclude='.cursor' \
    --exclude='.claude' \
    --exclude='.venv' \
    --exclude='.pytest_cache' \
    --exclude='__pycache__' \
    --exclude='build' \
    --exclude='scripts' \
    --exclude='data' \
    --exclude='*.pyc' \
    --exclude='.DS_Store' \
    "${ROOT}/" "$STAGING/"

find "$STAGING" -name '__pycache__' -type d -exec rm -rf {} + 2>/dev/null || true
find "$STAGING" -name '*.pyc' -delete 2>/dev/null || true

zip_name="${PLUGIN_SLUG}-${version}.zip"
zip_path="${ROOT}/build/${zip_name}"
rm -f "$zip_path"

( cd "${ROOT}/build" && zip -r "$zip_path" "$PLUGIN_SLUG" \
    -x "${PLUGIN_SLUG}/__pycache__/*" \
    -x "${PLUGIN_SLUG}/*/__pycache__/*" \
    -x "${PLUGIN_SLUG}/*/*/__pycache__/*" \
    -x "*.pyc" \
    -x ".DS_Store" )

rm -rf "$STAGING"

echo ""
echo "=== Built ${zip_path} (v${version}) ==="
echo ""
echo "Publish this ZIP as an asset on your GitHub/Gitea Release (not committed to git)."
echo "Install in QGIS: Plugin Manager → Install from ZIP → select ${zip_name}"
