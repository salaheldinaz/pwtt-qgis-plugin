#!/usr/bin/env bash
# Create a release and upload a ZIP using only GITHUB_API_URL (runner-visible host).
# Avoids Gitea upload_url pointing at an internal hostname (e.g. gitea.lan) that
# the job runner cannot resolve.
#
# Env:
#   GITHUB_API_URL     e.g. https://forge.example.com/api/v1 or https://api.github.com
#   GITHUB_REPOSITORY  owner/name
#   GITHUB_TOKEN       (or TOKEN) forge token
#   TAG                tag_name, e.g. v0.6.0
#   RELEASE_NAME       display name (default: TAG)
#   BODY_FILE          path to Markdown/text body
#   ZIP                path to the attachment
set -euo pipefail

API_URL="${GITHUB_API_URL:?Set GITHUB_API_URL}"
API_URL="${API_URL%/}"
REPO="${GITHUB_REPOSITORY:?}"
TOKEN="${GITHUB_TOKEN:-${TOKEN:-}}"
TOKEN="${TOKEN:?Set GITHUB_TOKEN or TOKEN}"
TAG="${TAG:?}"
RELEASE_NAME="${RELEASE_NAME:-$TAG}"
BODY_FILE="${BODY_FILE:?}"
ZIP="${ZIP:?}"

[[ -f "$BODY_FILE" ]] || { echo "Missing body file: $BODY_FILE" >&2; exit 1; }
[[ -f "$ZIP" ]] || { echo "Missing zip: $ZIP" >&2; exit 1; }

owner="${REPO%%/*}"
repo="${REPO#*/}"
zip_base="$(basename "$ZIP")"

tmp_payload="$(mktemp)"
jq -n \
  --rawfile body "$BODY_FILE" \
  --arg tag_name "$TAG" \
  --arg name "$RELEASE_NAME" \
  '{tag_name:$tag_name,name:$name,body:$body,draft:false,prerelease:false}' >"$tmp_payload"

if [[ "$API_URL" == *"/api/v1"* ]] || [[ "$API_URL" == *"api/v1"* ]]; then
  auth_h="Authorization: token ${TOKEN}"
  accept_h="Accept: application/json"
else
  auth_h="Authorization: Bearer ${TOKEN}"
  accept_h="Accept: application/vnd.github+json"
fi

echo "Creating release ${TAG}..."
resp_headers="$(mktemp)"
resp_body="$(mktemp)"
http_code="$(
  curl -sS -o "$resp_body" -D "$resp_headers" -w '%{http_code}' -X POST \
    -H "$auth_h" \
    -H "$accept_h" \
    -H 'Content-Type: application/json' \
    --data @"$tmp_payload" \
    "${API_URL}/repos/${owner}/${repo}/releases"
)"
rm -f "$tmp_payload"

if [[ "$http_code" != "201" ]]; then
  echo "Create release failed HTTP ${http_code}" >&2
  cat "$resp_body" >&2 || true
  rm -f "$resp_headers" "$resp_body"
  exit 1
fi

release_id="$(jq -r .id <"$resp_body")"

if [[ "$API_URL" == *"/api/v1"* ]] || [[ "$API_URL" == *"api/v1"* ]]; then
  # Gitea: multipart field "attachment", query name=filename (same host as API).
  enc_name="$(jq -rn --arg n "$zip_base" '$n|@uri')"
  echo "Uploading ${zip_base} to Gitea release ${release_id}..."
  curl -sS -f -X POST \
    -H "Authorization: token ${TOKEN}" \
    -F "attachment=@${ZIP};type=application/zip" \
    "${API_URL}/repos/${owner}/${repo}/releases/${release_id}/assets?name=${enc_name}"
else
  # GitHub: uploads.github.com URL from create response (not Gitea ROOT_URL).
  upload_url="$(jq -r .upload_url <"$resp_body")"
  upload_url="${upload_url%\{*}"
  enc_name="$(jq -rn --arg n "$zip_base" '$n|@uri')"
  echo "Uploading ${zip_base} to GitHub..."
  curl -sS -f -X POST \
    -H "Authorization: Bearer ${TOKEN}" \
    -H 'Content-Type: application/octet-stream' \
    --data-binary @"${ZIP}" \
    "${upload_url}?name=${enc_name}"
fi

rm -f "$resp_headers" "$resp_body"
echo "Release ${TAG} published with ${zip_base}."
