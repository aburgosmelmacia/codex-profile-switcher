#!/usr/bin/env bash

set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TEST_ROOT="$(mktemp -d)"
trap 'rm -rf "$TEST_ROOT"' EXIT

PROFILE_STORE="$TEST_ROOT/profile-store"
ACCOUNTS_HOME="$PROFILE_STORE/accounts"
AUTH_FILE="$TEST_ROOT/codex/auth.json"
FAKE_CODEX="$TEST_ROOT/codex-real"
mkdir -p "$ACCOUNTS_HOME" "$(dirname "$AUTH_FILE")"

cat >"$ACCOUNTS_HOME/shared.auth.json" <<'EOF'
{
  "auth_mode": "chatgpt",
  "tokens": {
    "access_token": "shared-access",
    "refresh_token": "central-shared-refresh",
    "account_id": "account-shared"
  }
}
EOF
cat >"$ACCOUNTS_HOME/secondary.auth.json" <<'EOF'
{
  "auth_mode": "chatgpt",
  "tokens": {
    "access_token": "secondary-access",
    "refresh_token": "central-secondary-refresh",
    "account_id": "account-secondary"
  }
}
EOF
cat >"$AUTH_FILE" <<'EOF'
{
  "auth_mode": "chatgpt",
  "tokens": {
    "access_token": "stale-access",
    "refresh_token": "stale-refresh",
    "account_id": "account-stale"
  }
}
EOF
printf '%s\n' shared >"$PROFILE_STORE/.default-profile"
printf '%s\n' shared >"$PROFILE_STORE/.current-profile"

cat >"$FAKE_CODEX" <<'EOF'
#!/usr/bin/env bash
set -euo pipefail
jq -e '.tokens.access_token != "" and .tokens.refresh_token == ""' "$CODEX_HOME/auth.json" >/dev/null
printf '%s\n' "$*" >"$CODEX_HOME/fake-codex-args"
EOF
chmod 755 "$FAKE_CODEX"

shared_before="$(sha256sum "$ACCOUNTS_HOME/shared.auth.json")"
secondary_before="$(sha256sum "$ACCOUNTS_HOME/secondary.auth.json")"

consumer() {
  CODEX_HOME="$(dirname "$AUTH_FILE")" \
  CODEX_PROFILE_STORE="$PROFILE_STORE" \
  CODEX_AUTH_PROFILES_HOME="$ACCOUNTS_HOME" \
  CODEX_CURRENT_PROFILE_FILE="$PROFILE_STORE/.current-profile" \
  CODEX_PROFILE_DEFAULT_FILE="$PROFILE_STORE/.default-profile" \
  AUTH_FILE="$AUTH_FILE" \
  CODEX_BIN="$FAKE_CODEX" \
  CODEX_PROFILE_CONSUMER=1 \
    "$ROOT/bin/codex-profile" "$@"
}

consumer run -- exec sample >/dev/null
jq -e '.tokens.access_token == "shared-access" and .tokens.refresh_token == ""' "$AUTH_FILE" >/dev/null
[[ "$(cat "$(dirname "$AUTH_FILE")/fake-codex-args")" == "exec sample" ]]
[[ "$shared_before" == "$(sha256sum "$ACCOUNTS_HOME/shared.auth.json")" ]]
[[ "$secondary_before" == "$(sha256sum "$ACCOUNTS_HOME/secondary.auth.json")" ]]

# Even when live auth byte-matches a saved source, consumer mode must restore a
# sanitized copy instead of taking the normal fast path.
cp "$ACCOUNTS_HOME/shared.auth.json" "$AUTH_FILE"
printf '%s\n' shared >"$PROFILE_STORE/.default-profile"
consumer run -- exec sanitize-match >/dev/null
jq -e '.tokens.access_token == "shared-access" and .tokens.refresh_token == ""' "$AUTH_FILE" >/dev/null

consumer use secondary >/dev/null
consumer run -- exec other >/dev/null
jq -e '.tokens.access_token == "secondary-access" and .tokens.refresh_token == ""' "$AUTH_FILE" >/dev/null
[[ "$(consumer current)" == "secondary" ]]

for command in "save shared" "login shared" "pi sync shared"; do
  if consumer $command >/dev/null 2>&1; then
    echo "consumer command unexpectedly succeeded: $command" >&2
    exit 1
  fi
done

echo "test-consumer-mode: ok"
