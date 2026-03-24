# codex-profile

Small shell tool to switch between Codex accounts without swapping `auth.json`.

Instead of copying credentials in and out of `~/.codex/auth.json`, `codex-profile`
creates one isolated `CODEX_HOME` per profile under `~/.codex-profiles`. That means
each account keeps its own:

- login
- session history
- cached state
- config

## Why this approach

The simplest community tools often swap `auth.json` snapshots. That works, but it
duplicates tokens and keeps all sessions mixed in a single `~/.codex` directory.

This tool takes the cleaner route:

- `personal` can live in `~/.codex-profiles/personal`
- `work` can live in `~/.codex-profiles/work`
- switching accounts is just running Codex with a different `CODEX_HOME`

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
codex-profile login personal
codex-profile login work
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

## Commands

```text
codex-profile list
codex-profile current
codex-profile use <profile>
codex-profile path <profile>
codex-profile login <profile>
codex-profile status [profile]
codex-profile run [profile] [-- <codex args...>]
codex-profile <profile> [-- <codex args...>]
```

## Notes

- Profiles are stored in `~/.codex-profiles`.
- On first use, the tool copies `~/.codex/config.toml` into the new profile if it exists.
- Profile names accept letters, numbers, dots, dashes, and underscores.
- This tool does not modify a running Codex process. If you already have a Codex session open, start a new one with the desired profile.

## Security

- This approach avoids rotating one shared `auth.json` in place.
- Credentials still live on disk, but they stay scoped to each profile directory.
- The tool tries to set restrictive filesystem permissions where possible.
