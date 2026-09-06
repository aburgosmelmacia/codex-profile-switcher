#!/usr/bin/env python3
"""Distribute access-only profiles over SSH; manage a consumer's Codex and Pi."""

import argparse
import base64
import contextlib
import datetime as dt
import fcntl
import json
import os
from pathlib import Path
import shlex
import subprocess
import sys
import time

PROFILES = ("shared", "secondary")


class AuthError(ValueError):
    """A deliberately credential-free operational error."""


def read_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (OSError, ValueError):
        raise AuthError("Cannot read a valid JSON configuration or credential file") from None


def claims(auth):
    try:
        raw = auth["tokens"]["access_token"].split(".")[1]
        value = json.loads(base64.urlsafe_b64decode(raw + "=" * (-len(raw) % 4)))
        if type(value["exp"]) is not int or type(value["iat"]) is not int:
            raise AuthError("Invalid access token timestamps")
        return value
    except (KeyError, IndexError, TypeError, ValueError, AssertionError):
        raise AuthError("Invalid access token metadata") from None


def sanitize(auth, account_id=None, minimum_ttl=300, receiving=False):
    try:
        tokens = auth["tokens"]
        account = tokens["account_id"]
        if not isinstance(account, str) or not account:
            raise AuthError("Missing account identity")
        if not isinstance(tokens["access_token"], str) or not tokens["access_token"]:
            raise AuthError("Missing access token")
        if receiving and (tokens.get("refresh_token") or auth.get("OPENAI_API_KEY")):
            raise AuthError("Renewal tokens and API keys are forbidden on consumers")
        if account_id is not None and account != account_id:
            raise AuthError("Profile does not match the configured account")
        meta = claims(auth)
        if meta["https://api.openai.com/auth"]["chatgpt_account_id"] != account:
            raise AuthError("Access token belongs to a different account")
        if meta["exp"] <= time.time() + minimum_ttl:
            raise AuthError("Access token expired or expiring; waiting for central synchronization")
        if meta["iat"] > time.time() + 60 or meta["iat"] >= meta["exp"]:
            raise AuthError("Invalid access token timestamps")
        identity = tokens.get("id_token", "")
        if not isinstance(identity, str):
            raise AuthError("Invalid identity token format")
    except (KeyError, TypeError, AssertionError):
        raise AuthError("Invalid account, expired token, or forbidden renewal credentials") from None
    return {
        "auth_mode": "chatgpt",
        "OPENAI_API_KEY": None,
        "tokens": {"access_token": tokens["access_token"], "account_id": account,
                   "id_token": identity, "refresh_token": ""},
        "last_refresh": auth.get("last_refresh"),
    }


def pi_block(auth):
    return {"type": "oauth", "access": auth["tokens"]["access_token"],
            "refresh": "", "accountId": auth["tokens"]["account_id"],
            "expires": claims(auth)["exp"] * 1000}


def check_target(path):
    path = Path(path)
    if any(p.is_symlink() for p in [path, *path.parents]):
        raise AuthError("Refusing a symlink credential target or parent")
    if path.exists() and not path.is_file():
        raise AuthError("Credential target is not a regular file")


class Consumer:
    def __init__(self, codex_home=None):
        self.home = Path(codex_home or os.environ.get("CODEX_SHARED_HOME") or os.environ.get("CODEX_HOME", Path.home() / ".codex"))
        self.store = Path(os.environ.get("CODEX_PROFILE_STORE", self.home / ".codex-profile"))
        self.accounts = Path(os.environ.get("CODEX_AUTH_PROFILES_HOME", self.store / "accounts"))
        self.auth_file = Path(os.environ.get("AUTH_FILE", self.home / "auth.json"))
        self.default_file = Path(os.environ.get("CODEX_PROFILE_DEFAULT_FILE", self.store / ".default-profile"))
        self.current_file = Path(os.environ.get("CODEX_CURRENT_PROFILE_FILE", self.store / ".current-profile"))
        self.marker = Path(os.environ.get("CODEX_PROFILE_CONSUMER_FILE", self.store / ".consumer"))
        self.config = read_json(self.store / "consumer.json")
        for environment, setting in (("PI_AUTH_FILE", "pi_auth"), ("CODEX_BIN", "codex_bin"), ("PI_BIN", "pi_bin")):
            if environment in os.environ:
                self.config[setting] = os.environ[environment]
        if set(self.config.get("account_ids", {})) != set(PROFILES):
            raise AuthError("Consumer must configure shared and secondary account IDs")

    @contextlib.contextmanager
    def lock(self):
        check_target(self.store / ".consumer-sync.lock")
        self.store.mkdir(parents=True, exist_ok=True, mode=0o700)
        with (self.store / ".consumer-sync.lock").open("a") as handle:
            os.fchmod(handle.fileno(), 0o600)
            fcntl.flock(handle, fcntl.LOCK_EX)
            yield

    def selection(self, current=False, initial=False):
        path = self.current_file if current else self.default_file
        value = path.read_text().strip() if path.exists() else "secondary"
        if value not in PROFILES:
            if initial:
                return "secondary"
            raise AuthError("Invalid selected consumer profile")
        return value

    def commit(self, auth, files):
        for target in [*files, Path(self.config["pi_auth"])]:
            check_target(target)
        bridge = Path(__file__).with_name("codex-consumer-pi.mjs")
        transaction = {
            "credential": pi_block(auth),
            "consumerMarker": str(self.marker),
            "files": [{"path": str(path), "text": value if isinstance(value, str)
                       else json.dumps(value, indent=2, sort_keys=True) + "\n"}
                      for path, value in files.items()],
        }
        result = subprocess.run(
            ["node", str(bridge), self.config["pi_package"], self.config["pi_auth"]],
            input=json.dumps(transaction), text=True, capture_output=True, timeout=15,
        )
        if result.returncode:
            raise AuthError("Credential transaction failed; check file access and Pi's auth lock")

    def receive(self, envelope):
        if not isinstance(envelope, dict) or envelope.get("version") != 1:
            raise AuthError("Unsupported synchronization message")
        incoming = envelope.get("profiles", {})
        if not isinstance(incoming, dict) or set(incoming) != set(PROFILES):
            raise AuthError("Both managed profiles must be delivered together")
        payloads = {p: sanitize(incoming[p], self.config["account_ids"][p], receiving=True)
                    for p in PROFILES}
        with self.lock():
            # Validate the whole batch before any auth files are touched.
            for target in (self.marker, self.default_file, self.store / "last-sync.json"):
                check_target(target)
            for p in PROFILES:
                old = self.accounts / (p + ".auth.json")
                check_target(old)
                check_target(self.accounts / (p + ".pi-auth.json"))
                if old.exists():
                    previous = claims(read_json(old))
                    new = claims(payloads[p])
                    if (new["iat"], new["exp"]) < (previous["iat"], previous["exp"]):
                        raise AuthError("Refusing an older profile snapshot")
            initial = not self.marker.exists()
            active = self.selection(current=True, initial=initial)
            # Install consumer mode before any credentials, including on interruption.
            files = {self.marker: "central-access-only\n"}
            for p in PROFILES:
                files[self.accounts / (p + ".auth.json")] = payloads[p]
                files[self.accounts / (p + ".pi-auth.json")] = {"openai-codex": pi_block(payloads[p])}
            files[self.auth_file] = payloads[active]
            files[self.current_file] = active + "\n"
            if initial or not self.default_file.exists():
                files[self.default_file] = active + "\n"
            files[self.store / "last-sync.json"] = {
                "received_at": dt.datetime.now(dt.timezone.utc).isoformat(),
                "active": active,
                "expires": {p: claims(payloads[p])["exp"] for p in PROFILES},
            }
            self.commit(payloads[active], files)
        return {"ok": True, "active": active}

    def activate(self, profile, persist=False):
        if profile not in PROFILES:
            raise AuthError("Choose shared or secondary")
        with self.lock():
            payload = sanitize(read_json(self.accounts / (profile + ".auth.json")),
                               self.config["account_ids"][profile], minimum_ttl=0, receiving=True)
            files = {self.auth_file: payload, self.current_file: profile + "\n"}
            if persist:
                files[self.default_file] = profile + "\n"
            self.commit(payload, files)

    def profile_command(self, args):
        args = list(args)
        command = args.pop(0) if args else "help"
        if command == "pi":
            sub = args.pop(0) if args else "help"
            if sub == "use" and len(args) == 1:
                return self.profile_command(["use", args[0]])
            if sub == "run":
                return self.launch("pi_bin", args)
            raise AuthError("Consumer Pi supports use and run; renewal is central")
        if command == "run":
            return self.launch("codex_bin", args)
        if command == "use" and len(args) == 1:
            self.activate(args[0], persist=True)
            print("Codex and Pi selected:", args[0])
        elif command == "current":
            print(self.selection())
        elif command == "list":
            for p in PROFILES:
                print(p + (" (default)" if p == self.selection() else ""))
        elif command == "status":
            print("Consumer mode; renewal is managed by the VPS")
            print("Default:", self.selection(), "Active:", self.selection(current=True))
            if (self.store / "last-sync.json").exists():
                print(json.dumps(read_json(self.store / "last-sync.json"), sort_keys=True))
        elif command == "config" and args == ["show"]:
            print("[auto_switch]\nenabled = false")
        elif command in ("help", "--help", "-h"):
            print("codex-profile list|current|status|use shared|secondary|run [profile] -- ARGS")
            print("codex-profile pi use PROFILE | pi run [PROFILE] -- ARGS")
        else:
            raise AuthError("Operation unavailable in access-only consumer mode")

    def launch(self, executable, args):
        if args and args[0] in PROFILES:
            profile, args = args[0], args[1:]
        else:
            profile = self.selection()
        if args and args[0] == "--":
            args = args[1:]
        # Disallow direct CLI login/logout through managed launchers as well.
        if executable == "codex_bin" and args and args[0] in ("login", "logout"):
            if args == ["login", "status"]:
                return self.profile_command(["status"])
            raise AuthError("Login and renewal are managed by the VPS")
        self.activate(profile)
        os.environ["CODEX_HOME"] = str(self.home)
        os.execv(self.config[executable], [self.config[executable], *args])


def publish(source_dir, host, receiver, timeout):
    payloads = {p: sanitize(read_json(Path(source_dir) / (p + ".auth.json"))) for p in PROFILES}
    envelope = json.dumps({"version": 1, "profiles": payloads})
    # Secrets only travel over encrypted stdin, never arguments or log messages.
    result = subprocess.run(
        ["ssh", "-o", "BatchMode=yes", "-o", "StrictHostKeyChecking=yes",
         "-o", "ConnectTimeout=8", "-o", "ServerAliveInterval=5",
         "-o", "ServerAliveCountMax=2", host, shlex.quote(receiver) + " receive"],
        input=envelope, text=True, capture_output=True, timeout=timeout,
    )
    if result.returncode:
        if result.returncode == 255:
            raise AuthError("SSH host unavailable; next scheduled run will retry")
        raise AuthError("Receiver rejected the delivery; check receiver configuration and token validity")
    try:
        receipt = json.loads(result.stdout)
        if receipt.get("ok") is not True or receipt.get("active") not in PROFILES:
            raise AuthError("Invalid delivery acknowledgment")
    except (ValueError, AttributeError, AssertionError):
        raise AuthError("Receiver did not confirm a successful delivery") from None
    print("Synced shared and secondary to", host, "; active=" + receipt["active"])


def main():
    if len(sys.argv) > 1 and sys.argv[1] == "profile":
        Consumer().profile_command(sys.argv[2:])
        return
    parser = argparse.ArgumentParser(description=__doc__)
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("receive")
    send = sub.add_parser("publish")
    send.add_argument("--source-dir", required=True)
    send.add_argument("--host", required=True)
    send.add_argument("--receiver", required=True)
    send.add_argument("--timeout", type=int, default=25)
    args = parser.parse_args()
    if args.command == "receive":
        raw = sys.stdin.read(131073)
        if len(raw) > 131072:
            raise AuthError("Synchronization message is too large")
        try:
            envelope = json.loads(raw)
        except ValueError:
            raise AuthError("Invalid synchronization message") from None
        print(json.dumps(Consumer().receive(envelope)))
    else:
        publish(args.source_dir, args.host, args.receiver, args.timeout)


if __name__ == "__main__":
    try:
        main()
    except AuthError as error:
        print("Consumer auth:", error, file=sys.stderr)
        sys.exit(1)
    except (ValueError, OSError, subprocess.SubprocessError):
        # Third-party exception text may include subprocess input or credentials.
        print("Consumer auth operation failed; check configuration, connectivity, token validity and file locks.", file=sys.stderr)
        sys.exit(1)
