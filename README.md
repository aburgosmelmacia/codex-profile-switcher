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
git clone <private-repo-url>
cd codex-profile
chmod +x bin/codex-profile
ln -sfn "$(pwd)/bin/codex-profile" ~/.local/bin/codex-profile
```

Make sure `~/.local/bin` is on your `PATH`.

## Quick start

Login once per account:

```bash
codex-profile save personal
codex-profile login personal
codex-profile login work
```

If you are already logged into one account in `~/.codex`, run `save` first so you
can keep that account as a named profile without logging in again.

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

## Commands

```text
codex-profile list
codex-profile current
codex-profile use <profile>
codex-profile path <profile>
codex-profile save <profile>
codex-profile login <profile>
codex-profile status [profile]
codex-profile run [profile] [-- <codex args...>]
codex-profile <profile> [-- <codex args...>]
```

## Notes

- Saved auth profiles are stored in `~/.codex/.codex-profile/accounts`.
- The live auth file remains `~/.codex/auth.json`.
- Profile names accept letters, numbers, dots, dashes, and underscores.
- `codex-profile save <profile>` snapshots the account already active in `~/.codex/auth.json`.
- `codex-profile login <profile>` always uses `codex login --device-auth`.
- This tool imports legacy auth automatically from `~/.codex-profiles/<profile>/auth.json` if present.
- This tool does not modify a running Codex process. If you already have a Codex session open, start a new one with the desired profile.

## Security

- This approach keeps one shared Codex state directory, but stores one credential snapshot per profile.
- Credentials still live on disk, so the account snapshot directory should remain private to your user.
- The tool tries to set restrictive filesystem permissions where possible.
