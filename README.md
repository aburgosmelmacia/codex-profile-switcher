# codex-profile

Small shell tool to switch between Codex accounts while keeping one shared Codex
workspace.

`codex-profile` keeps:

- one shared `CODEX_HOME`, usually `~/.codex`
- one shared set of sessions
- one shared set of skills
- one shared config/history/cache
- one saved `auth.json` snapshot per account

The `login` command in this tool always uses Codex device auth, and the `run`
command swaps the live `auth.json` before launching Codex.

It can also auto-switch between saved profiles when the active one falls below
configured weekly or 5-hour thresholds.

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
```

Typical first-time flow when one account is already active:

```bash
codex-profile save personal
codex-profile login work
codex-profile use personal
codex-profile install-wrapper
codex-profile setup
```

Under the hood, those commands run:

```bash
CODEX_HOME=~/.codex codex login --device-auth
```

Optionally set a default profile:

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
- after each `codex-profile run`, the selected profile's cache is refreshed from the latest session files
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
codex-profile run [profile] [-- <codex args...>]
codex-profile <profile> [-- <codex args...>]
```

## Notes

- Saved auth profiles are stored in `~/.codex/.codex-profile/accounts`.
- Cached per-profile limits are stored in `~/.codex/.codex-profile/limits`.
- `codex-profile limits` shows the last known weekly/5-hour values per profile.
- `codex-profile limits refresh` forces a live refresh before showing the table.
- `codex-profile setup` opens the same TUI as `codex-profile config tui`.
- `codex-profile install-wrapper` creates a safe `codex` wrapper plus `codex-real`.
- The live auth file remains `~/.codex/auth.json`.
- Auto-switch config is stored in `~/.codex/.codex-profile/config.toml`.
- Profile names accept letters, numbers, dots, dashes, and underscores.
- `codex-profile save <profile>` snapshots the account already active in `~/.codex/auth.json`.
- `codex-profile login <profile>` always uses `codex login --device-auth`.
- Auto-switch applies only to `codex-profile run` without an explicit profile.
- `codex-profile config tui` requires `whiptail` to be installed.
- This tool imports legacy auth automatically from `~/.codex-profiles/<profile>/auth.json` if present.
- This tool does not modify a running Codex process. If you already have a Codex session open, start a new one with the desired profile.
- Use `codex-profile install-wrapper --force` if you intentionally want to replace an existing `codex` wrapper or `codex-real` link.

## Security

- This approach keeps one shared Codex state directory, but stores one credential snapshot per profile.
- Credentials still live on disk, so the account snapshot directory should remain private to your user.
- Auto-switch refreshes limits with short isolated Codex runs and falls back to cached local session data if needed.
- The tool tries to set restrictive filesystem permissions where possible.
