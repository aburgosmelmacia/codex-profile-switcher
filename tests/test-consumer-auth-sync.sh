#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

SOURCE="$TEST_ROOT/source"
CONSUMER_HOME="$TEST_ROOT/consumer"
mkdir -p "$SOURCE" "$CONSUMER_HOME/.codex/.codex-profile"
printf '%s\n' secondary >"$CONSUMER_HOME/.codex/.codex-profile/.current-profile"

payload="$(printf '%s' '{"exp":1893456000}' | base64 -w0 | tr '+/' '-_' | tr -d '=')"
access_token="header.${payload}.signature"
for profile in shared secondary; do
  cat >"$SOURCE/$profile.auth.json" <<EOF
{
  "auth_mode": "chatgpt",
  "tokens": {
    "access_token": "$access_token-$profile",
    "refresh_token": "central-$profile-refresh",
    "account_id": "account-$profile"
  }
}
EOF
done

shared_before="$(sha256sum "$SOURCE/shared.auth.json")"
secondary_before="$(sha256sum "$SOURCE/secondary.auth.json")"

"$ROOT/bin/codex-consumer-auth-sync" \
  --source-dir "$SOURCE" \
  --consumer "test:$CONSUMER_HOME" \
  --lock-file "$TEST_ROOT/sync.lock" \
  --no-chown >/dev/null

for profile in shared secondary; do
  target="$CONSUMER_HOME/.codex/.codex-profile/accounts/$profile.auth.json"
  jq -e --arg profile "$profile" \
    '.tokens.refresh_token == "" and .tokens.account_id == ("account-" + $profile)' \
    "$target" >/dev/null
  [[ "$(stat -c %a "$target")" == "600" ]]
done
jq -e '.tokens.refresh_token == "" and .tokens.account_id == "account-secondary"' \
  "$CONSUMER_HOME/.codex/auth.json" >/dev/null
[[ "$(cat "$CONSUMER_HOME/.codex/.codex-profile/.consumer")" == "central-access-only" ]]
[[ "$shared_before" == "$(sha256sum "$SOURCE/shared.auth.json")" ]]
[[ "$secondary_before" == "$(sha256sum "$SOURCE/secondary.auth.json")" ]]

printf '%s\n' untouched >"$TEST_ROOT/victim"
ln -sfn "$TEST_ROOT/victim" "$CONSUMER_HOME/.codex/auth.json"
if "$ROOT/bin/codex-consumer-auth-sync" \
  --source-dir "$SOURCE" \
  --consumer "test:$CONSUMER_HOME" \
  --lock-file "$TEST_ROOT/sync.lock" \
  --no-chown >/dev/null 2>&1; then
  echo "sync unexpectedly accepted a symlink target" >&2
  exit 1
fi
[[ "$(cat "$TEST_ROOT/victim")" == "untouched" ]]

echo "test-consumer-auth-sync: ok"
