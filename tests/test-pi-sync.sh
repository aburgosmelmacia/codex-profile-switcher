#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

PROFILE_STORE="$TEST_ROOT/profile-store"
ACCOUNTS_HOME="$PROFILE_STORE/accounts"
PI_AUTH_FILE="$TEST_ROOT/pi/auth.json"
AUTH_FILE="$TEST_ROOT/codex/auth.json"
mkdir -p "$ACCOUNTS_HOME" "$(dirname "$PI_AUTH_FILE")" "$(dirname "$AUTH_FILE")"

payload="$(printf '%s' '{"exp":1893456000}' | base64 -w0 | tr '+/' '-_' | tr -d '=')"
access_token="header.${payload}.signature"

cat >"$ACCOUNTS_HOME/shared.auth.json" <<EOF
{
  "auth_mode": "chatgpt",
  "tokens": {
    "access_token": "$access_token",
    "refresh_token": "fresh-refresh",
    "account_id": "account-shared"
  }
}
EOF
cat >"$ACCOUNTS_HOME/fallback.auth.json" <<'EOF'
{
  "auth_mode": "chatgpt",
  "tokens": {
    "access_token": "header.%.signature",
    "refresh_token": "fallback-refresh",
    "account_id": "account-fallback",
    "expires_at": 1900000000
  }
}
EOF
printf '%s\n' shared >"$PROFILE_STORE/.current-profile"
printf '%s\n' '{"other":{"type":"api_key","key":"preserve-me"}}' >"$PI_AUTH_FILE"

CODEX_PROFILE_STORE="$PROFILE_STORE" \
CODEX_AUTH_PROFILES_HOME="$ACCOUNTS_HOME" \
CODEX_CURRENT_PROFILE_FILE="$PROFILE_STORE/.current-profile" \
AUTH_FILE="$AUTH_FILE" \
PI_AUTH_FILE="$PI_AUTH_FILE" \
  "$ROOT/bin/codex-profile" pi convert shared >/dev/null

CODEX_PROFILE_STORE="$PROFILE_STORE" \
CODEX_AUTH_PROFILES_HOME="$ACCOUNTS_HOME" \
CODEX_CURRENT_PROFILE_FILE="$PROFILE_STORE/.current-profile" \
AUTH_FILE="$AUTH_FILE" \
PI_AUTH_FILE="$PI_AUTH_FILE" \
  "$ROOT/bin/codex-profile" pi use shared >/dev/null

CODEX_PROFILE_STORE="$PROFILE_STORE" \
CODEX_AUTH_PROFILES_HOME="$ACCOUNTS_HOME" \
CODEX_CURRENT_PROFILE_FILE="$PROFILE_STORE/.current-profile" \
AUTH_FILE="$AUTH_FILE" \
PI_AUTH_FILE="$PI_AUTH_FILE" \
  "$ROOT/bin/codex-profile" pi sync shared >/dev/null

printf '%s\n' fallback >"$PROFILE_STORE/.current-profile"
CODEX_PROFILE_STORE="$PROFILE_STORE" \
CODEX_AUTH_PROFILES_HOME="$ACCOUNTS_HOME" \
CODEX_CURRENT_PROFILE_FILE="$PROFILE_STORE/.current-profile" \
AUTH_FILE="$AUTH_FILE" \
PI_AUTH_FILE="$PI_AUTH_FILE" \
  "$ROOT/bin/codex-profile" pi sync fallback >/dev/null

python3 - "$ACCOUNTS_HOME/shared.pi-auth.json" "$ACCOUNTS_HOME/fallback.pi-auth.json" "$PI_AUTH_FILE" <<'PY'
import json, sys
snapshot = json.load(open(sys.argv[1], encoding="utf-8"))
fallback = json.load(open(sys.argv[2], encoding="utf-8"))
live = json.load(open(sys.argv[3], encoding="utf-8"))
expected = 1893456000 * 1000
assert snapshot["openai-codex"]["expires"] == expected
assert fallback["openai-codex"]["expires"] == 1900000000 * 1000
assert live["openai-codex"]["expires"] == expected
assert live["openai-codex"]["refresh"] == "fresh-refresh"
assert live["other"]["key"] == "preserve-me"
PY

echo "test-pi-sync: ok"
