# codex-profile

Small shell tool to switch between Codex accounts while keeping one shared Codex
workspace.

`codex-profile` keeps:

- one shared `CODEX_HOME`, usually `~/.codex`
- one shared set of sessions
- one shared set of skills
- one shared config/history/cache
- one saved `auth.json` snapshot per account
- optionally, one saved Pi `openai-codex` auth snapshot per account

The `login` command in this tool always uses Codex device auth, and the `run`
command swaps the live `auth.json` before launching Codex.

It can also auto-switch between saved profiles when the active one falls below
configured weekly or 5-hour thresholds.

For Pi agent, `codex-profile pi ...` only swaps the `openai-codex` block in
`~/.pi/agent/auth.json`; other providers such as DeepSeek or OpenRouter are left
untouched.

## Why this approach

Using a separate `CODEX_HOME` per account makes authentication easy, but it also
separates your sessions and local state. If you want to continue old threads no
matter which account you are currently using, shared state works better.

This tool therefore uses:

- shared state in `~/.codex`
- saved auth profiles in `~/.codex/.codex-profile/accounts`
- fast account switching by restoring the chosen auth snapshot

## Installation

```bash
sudo apt-get update
sudo apt-get install -y jq whiptail

mkdir -p ~/.local/bin
git clone https://github.com/aburgosmelmacia/codex-profile-switcher.git ~/codex-profile-switcher
cd ~/codex-profile-switcher
chmod +x bin/codex-profile
ln -sfn ~/codex-profile-switcher/bin/codex-profile ~/.local/bin/codex-profile
```

Make sure `~/.local/bin` is on your `PATH`.

If you want the visible `codex` command to launch through this tool safely:

```bash
codex-profile install-wrapper
```

That command creates:

- `~/.local/bin/codex` as a generated wrapper that calls `codex-profile run`
- `~/.local/bin/codex-real` as a preserved handle to the real Codex CLI

This is intentionally a wrapper, not a direct symlink to `codex-profile`, so
internal calls do not recurse back into the switcher.

Dependencies:

- `jq` is required for live limit refresh and auto-switch.
- `whiptail` is required for `codex-profile setup`.

## Quick start

If you already have one account active in `~/.codex`, save it first:

```bash
codex-profile save personal
```

If you are starting from scratch and want to name two accounts directly:

```bash
codex-profile login personal
codex-profile login work
codex-profile use personal
```

Typical first-time flow when one account is already active:

```bash
codex-profile save personal
codex-profile login work
codex-profile use personal
codex-profile install-wrapper
codex-profile setup
```

Under the hood, `login` runs Codex device auth in an isolated temporary `CODEX_HOME`, saves only the resulting `auth.json` snapshot, and leaves the live shared auth unchanged until you run `use` or `run`.

Activate a profile and set it as the default (also updates the Pi Codex auth block;
if needed, it builds that profile's Pi snapshot first):

```bash
codex-profile use work
```

Run Codex with the default profile:

```bash
codex-profile run
```

Or, after installing the wrapper:

```bash
codex
```

Run Codex with an explicit profile:

```bash
codex-profile run personal
```

Shortcut form:

```bash
codex-profile personal
```

Pass arguments through to Codex:

```bash
codex-profile personal -- resume --last
codex-profile work -- exec "review this repo"
```

Because state is shared, `resume --last` keeps seeing the same session index.

## Auto-switch

`codex-profile` can evaluate saved profiles automatically on `run` when no
explicit profile is provided. This lets you keep using the same command while
the tool jumps away from the current account if its remaining limits are too low.
On each auto-switch run, it performs a short non-interactive Codex refresh for
the relevant profiles, reads the resulting `rate_limits`, and keeps a local
cache under `~/.codex/.codex-profile/limits` as a fallback.

Enable it:

```bash
codex-profile config set auto_switch.enabled true
codex-profile config set auto_switch.weekly_threshold_percent 20
codex-profile config set auto_switch.five_hour_threshold_percent 20
```

Show the effective config:

```bash
codex-profile config show
```

Or edit it from the setup TUI:

```bash
codex-profile setup
```

`codex-profile config tui` remains available as an alias.

Refresh the known limits for every saved profile:

```bash
codex-profile limits refresh
```

Behavior:

- the current profile is refreshed first with a short non-interactive probe
- if it is above threshold, it stays active
- if it falls below threshold, the tool refreshes the other saved profiles and compares them
- selection prefers higher weekly remaining first, then higher 5-hour remaining
- limit caches are refreshed from isolated live probes, not from the shared session history
- if a live probe fails, the tool falls back to the last cached limits for that profile

Trade-offs:

- auto-switch adds a bit of startup latency because it probes limits live
- those live probes are real Codex runs, so they may consume a small amount of usage

Explicit profile runs such as `codex-profile melmacia2` do not auto-switch away.

## Commands

```text
codex-profile install-wrapper [--force] [--wrapper-path PATH] [--real-path PATH] [--codex-bin PATH]
codex-profile list
codex-profile limits
codex-profile limits refresh [profile]
codex-profile setup
codex-profile current
codex-profile use <profile>
codex-profile path <profile>
codex-profile save <profile>
codex-profile login <profile>
codex-profile status [profile]
codex-profile config show
codex-profile config set <key> <value>
codex-profile config tui
codex-profile pi save <profile>
codex-profile pi use <profile>
codex-profile pi run [profile] [-- <pi args...>]
codex-profile pi login <profile>
codex-profile pi convert <profile>
codex-profile run [profile] [-- <codex args...>]
codex-profile <profile> [-- <codex args...>]
```

## Pi agent

Pi uses its own OAuth file at `~/.pi/agent/auth.json`, so its first login is
manual:

```bash
pi
codex-profile pi save work
codex-profile pi run work
```

Useful commands:

- `codex-profile pi save <profile>` saves only the current `openai-codex` block.
- `codex-profile pi use <profile>` restores Codex auth and Pi `openai-codex` auth.
- `codex-profile pi run <profile>` restores both, then runs `pi`.
- `codex-profile pi login <profile>` prints the manual-login reminder.
- `codex-profile pi convert <profile>` creates a Pi snapshot from a saved Codex snapshot.

## Notes

- Saved auth profiles are stored in `~/.codex/.codex-profile/accounts`.
- Cached per-profile limits are stored in `~/.codex/.codex-profile/limits`.
- `codex-profile limits` shows the last known weekly/5-hour values per profile.
- `codex-profile limits refresh` forces a live refresh before showing the table.
- `codex-profile setup` opens the same TUI as `codex-profile config tui`.
- `codex-profile install-wrapper` creates a safe `codex` wrapper plus `codex-real`.
- The live auth file remains `~/.codex/auth.json`.
- If `~/.codex/auth.json` is a symlink, profile restores update the symlink target and keep the symlink intact.
- Auto-switch config is stored in `~/.codex/.codex-profile/config.toml`.
- Profile names accept letters, numbers, dots, dashes, and underscores.
- `codex-profile save <profile>` snapshots the account already active in `~/.codex/auth.json`.
- `codex-profile login <profile>` uses isolated `codex login --device-auth`, saves `<profile>.auth.json`, and does not modify the live auth.
- Pi auth snapshots are stored beside Codex snapshots as `<profile>.pi-auth.json`.
- Auto-switch applies only to `codex-profile run`/`codex-profile pi run` without an explicit profile.
- `codex-profile config tui` requires `whiptail` to be installed.
- This tool imports legacy auth automatically from `~/.codex-profiles/<profile>/auth.json` if present.
- Existing interactive Codex sessions are not switched. Open a new session after selecting a different account.
- Successful auth changes invalidate a stale managed `app-server` daemon before starting a new session (see below).
- Use `codex-profile install-wrapper --force` if you intentionally want to replace an existing `codex` wrapper or `codex-real` link.

## Managed app-server freshness (Linux)

A long-lived Codex `app-server` daemon can keep using the previous account even after
`auth.json` changes. Successful auth changes now check its identity and freshness;
`run -- login` and `run -- logout` also perform the check after Codex exits.

- A daemon is fresh only if it started **strictly after** the effective auth file's mtime.
- Signalling requires a verified `app-server` command, boot ID, process start ticks and
  Linux pidfd. Only absence or a zombie confirms that the old process stopped.
- An unverifiable or surviving stale daemon blocks a new launch. The explicit escape
  `CODEX_PROFILE_ALLOW_STALE_DAEMON=1` bypasses that block; use it only if you knowingly
  accept that the new session may use stale credentials.
- Interactive launches and Pi launches require an `AUTH_FILE` override to refer to
  the effective shared `CODEX_HOME/auth.json`. The normal variant deliberately exempts
  `exec`/`login`/`logout` one-shots (which still check/invalidate the managed daemon).
  The consumer variant enforces coupling in all launch modes.

## Centrally managed consumers

`bin/codex-profile-consumer` preserves the separately reviewed system variant used by
access-only consumers. It is **not** the default binary and is not installed automatically.
The two variants deliberately differ: normal `use` activates Codex and Pi transactionally;
consumer `use` sets the default without changing live credentials. Do not replace a
consumer installation with the normal binary.

Consumer mode is selected by `CODEX_PROFILE_CONSUMER=1` or the `.consumer` marker in the
profile store. Snapshots are supplied by an external central distributor; no distributor,
auth file, token or host-specific service configuration is included in this repository.
Consumer mode prevents saving live auth back to snapshots and denies consumer-side
credential publication/Pi switching. The system variant also requires the live auth file
to be owned by the consumer with exact mode `0600`, and its directory to be `0700`.

Installing the binary alone does **not** enable consumer mode. Before deployment,
ensure every consumer wrapper exports `CODEX_PROFILE_CONSUMER=1`, or that the central
distributor creates the `.consumer` marker in each consumer's profile store. Without
that environment/marker, consumer-only protections do not apply.

To install that variant deliberately (review your existing installation first):

```bash
sudo install -m 0755 bin/codex-profile-consumer /usr/local/bin/codex-profile
```

## Tests

All credentials and daemons used by these checks are synthetic. Tests do not contact
Codex or change an existing user's profile.

```bash
python3 -m unittest discover -s tests -p 'test_codex_profiles.py'
bash tests/test_consumer_daemon.sh
```

These tests require GNU/Linux (`/proc`, pidfds and GNU utilities). The normal variant
has 61 contract tests; the consumer daemon harness has 42 cases. Cross-user cases use
non-interactive sudo when available, otherwise report skips. Two cgroup-freezer tests
are skipped unless explicitly enabled with `CODEX_PROFILE_TEST_ROOT_CGROUP=1` on a
throwaway host: they create a temporary root process/cgroup and cannot guarantee
cleanup after a hard kill. CI deliberately leaves that opt-in unset on ephemeral
GitHub-hosted runners with read-only repository permissions.

The consumer variant conservatively invalidates daemon metadata it cannot parse;
English weekday/month metadata under a non-English locale may therefore cause extra
invalidation. Its test harness fixes `LC_ALL=C`; no installed binary is changed here.
An account-changing bare `codex login` may also make the normal variant reject an
implicit snapshot refresh, rather than overwrite a saved account. Save the new account
under its own profile name before continuing; this fail-closed behavior is intentional.
Seven integration tests for the operator's external token manager remain on the host;
that unrelated script is intentionally not included here.

## Security

- This approach keeps one shared Codex state directory, but stores one credential snapshot per profile.
- Credentials still live on disk, so the account snapshot directory should remain private to your user.
- Auto-switch refreshes limits with short isolated Codex runs and falls back to cached local session data if needed.
- The tool tries to set restrictive filesystem permissions where possible.
