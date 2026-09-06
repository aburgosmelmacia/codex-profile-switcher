import base64
import copy
import importlib.util
import json
import os
from pathlib import Path
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from unittest.mock import patch

ROOT = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("consumer", ROOT / "bin/codex-consumer-auth.py")
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


def credential(profile, age=10, remaining=3600):
    meta = {"iat": int(time.time()) - age, "exp": int(time.time()) + remaining,
            "https://api.openai.com/auth": {"chatgpt_account_id": profile + "-id"}}
    encoded = base64.urlsafe_b64encode(json.dumps(meta).encode()).decode().rstrip("=")
    return {"tokens": {"access_token": "fake." + encoded + ".fixture", "refresh_token": "",
                       "account_id": profile + "-id", "id_token": "fixture-identity"}}


class ConsumerTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.home = Path(self.temp.name)
        self.codex = self.home / ".codex"
        self.store = self.codex / ".codex-profile"
        self.store.mkdir(parents=True)
        self.pi = self.home / ".pi/agent/auth.json"
        self.pi.parent.mkdir(parents=True)
        self.pi.write_text(json.dumps({"another-provider": {"type": "api_key", "key": "fixture-other"}}))
        self.config = {"account_ids": {p: p + "-id" for p in module.PROFILES},
                       "pi_package": str(ROOT), "pi_auth": str(self.pi),
                       "codex_bin": "/bin/true", "pi_bin": "/bin/true"}
        (self.store / "consumer.json").write_text(json.dumps(self.config))
        self.client = module.Consumer(self.codex)
        self.envelope = {"version": 1, "profiles": {p: credential(p) for p in module.PROFILES}}

    def snapshot(self):
        return {str(p.relative_to(self.home)): p.read_bytes() for p in self.home.rglob("*")
                if p.is_file() and p.name != ".consumer-sync.lock"}

    def test_receive_same_accounts_and_no_renewal(self):
        receipt = self.client.receive(self.envelope)
        self.assertEqual(receipt, {"ok": True, "active": "secondary"})
        for p in module.PROFILES:
            saved = module.read_json(self.client.accounts / (p + ".auth.json"))
            self.assertEqual(saved["tokens"]["access_token"], self.envelope["profiles"][p]["tokens"]["access_token"])
            self.assertEqual(saved["tokens"]["refresh_token"], "")
        live = module.read_json(self.pi)
        self.assertEqual(live["another-provider"]["key"], "fixture-other")
        self.assertEqual(live["openai-codex"]["refresh"], "")
        self.assertEqual(live["openai-codex"]["expires"], module.claims(self.envelope["profiles"]["secondary"])["exp"] * 1000)
        for p in [self.codex / "auth.json", self.pi, *self.client.accounts.glob("*.json")]:
            self.assertEqual(stat.S_IMODE(p.stat().st_mode), 0o600)

    def test_invalid_batch_does_not_touch_either_agent(self):
        for mutation in ("renewal", "wrong-account", "expired", "missing-profile"):
            with self.subTest(mutation=mutation):
                envelope = copy.deepcopy(self.envelope)
                if mutation == "renewal":
                    envelope["profiles"]["shared"]["tokens"]["refresh_token"] = "must-not-travel"
                elif mutation == "wrong-account":
                    envelope["profiles"]["shared"] = credential("secondary")
                elif mutation == "expired":
                    envelope["profiles"]["shared"] = credential("shared", remaining=-1)
                else:
                    del envelope["profiles"]["shared"]
                before = self.snapshot()
                with self.assertRaises(module.AuthError):
                    self.client.receive(envelope)
                self.assertEqual(self.snapshot(), before)

    def test_older_snapshot_rejected_without_changes(self):
        self.client.receive(self.envelope)
        before = self.snapshot()
        old = copy.deepcopy(self.envelope)
        old["profiles"]["secondary"] = credential("secondary", age=100, remaining=3500)
        with self.assertRaises(module.AuthError):
            self.client.receive(old)
        self.assertEqual(self.snapshot(), before)

    def test_optimized_python_still_rejects_wrong_accounts_and_renewal(self):
        for kind in ("wrong-account", "renewal", "expired"):
            with self.subTest(kind=kind):
                bad = copy.deepcopy(self.envelope)
                if kind == "wrong-account":
                    bad["profiles"]["shared"] = credential("secondary")
                elif kind == "expired":
                    bad["profiles"]["shared"] = credential("shared", remaining=-1)
                else:
                    bad["profiles"]["shared"]["tokens"]["refresh_token"] = "secret-must-not-print"
                before = self.snapshot()
                result = subprocess.run([sys.executable, "-O", str(ROOT / "bin/codex-consumer-auth.py"), "receive"],
                    env={**os.environ, "CODEX_HOME": str(self.codex)}, input=json.dumps(bad),
                    text=True, capture_output=True)
                self.assertNotEqual(result.returncode, 0)
                self.assertNotIn("secret-must-not-print", result.stdout + result.stderr)
                self.assertEqual(self.snapshot(), before)

    def test_initial_delivery_replaces_legacy_selection(self):
        (self.store / ".current-profile").write_text("legacy-account\n")
        (self.store / ".default-profile").write_text("legacy-account\n")
        self.client.receive(self.envelope)
        self.assertEqual(self.client.selection(), "secondary")
        self.assertEqual(self.client.selection(current=True), "secondary")

    def test_selection_remains_local_on_subsequent_delivery(self):
        self.client.receive(self.envelope)
        self.client.activate("shared", persist=True)
        self.client.receive(self.envelope)
        self.assertEqual(self.client.selection(), "shared")
        self.assertEqual(module.read_json(self.codex / "auth.json")["tokens"]["account_id"], "shared-id")
        self.assertEqual(module.read_json(self.pi)["openai-codex"]["accountId"], "shared-id")

    def test_newer_delivery_updates_both_agents(self):
        self.client.receive(self.envelope)
        new = {"version": 1, "profiles": {p: credential(p, age=0, remaining=5000) for p in module.PROFILES}}
        self.client.receive(new)
        token = new["profiles"]["secondary"]["tokens"]["access_token"]
        self.assertEqual(module.read_json(self.pi)["openai-codex"]["access"], token)
        self.assertEqual(module.read_json(self.codex / "auth.json")["tokens"]["access_token"], token)

    def test_pi_lock_prevents_racing_an_interactive_writer(self):
        writer = subprocess.Popen(["node", "--input-type=module", "-e", """
import lockfile from 'proper-lockfile';
import fs from 'node:fs/promises';
const target = process.argv[1];
const release = await lockfile.lock(target, {realpath:false});
console.log('locked');
await new Promise(r => setTimeout(r, 400));
const value = JSON.parse(await fs.readFile(target, 'utf8'));
value.concurrent = {type:'api_key',key:'other-edit'};
await fs.writeFile(target, JSON.stringify(value));
await release();
""", str(self.pi)], cwd=ROOT, stdout=subprocess.PIPE, text=True)
        self.assertEqual(writer.stdout.readline().strip(), "locked")
        self.client.receive(self.envelope)
        self.assertEqual(writer.wait(timeout=5), 0)
        writer.stdout.close()
        self.assertEqual(module.read_json(self.pi)["concurrent"]["key"], "other-edit")

    def test_broken_pi_auth_is_not_overwritten(self):
        self.pi.write_text("invalid-json")
        before = self.snapshot()
        with self.assertRaises(module.AuthError):
            self.client.receive(self.envelope)
        self.assertEqual(self.snapshot(), before)

    def test_symlink_cannot_cause_a_partial_credential_update(self):
        external = self.home / "external.json"
        external.write_text("original")
        (self.codex / "auth.json").symlink_to(external)
        before = self.snapshot()
        with self.assertRaises(module.AuthError):
            self.client.receive(self.envelope)
        self.assertEqual(self.snapshot(), before)

    def test_failed_replacement_rolls_back_every_target(self):
        for initial in (True, False):
            with self.subTest(initial=initial):
                if not initial:
                    self.client.receive(self.envelope)
                injector = self.home / "fail-rename.mjs"
                injector.write_text("""
import { promises as fs } from 'node:fs';
const rename = fs.rename;
let failed = false;
fs.rename = async (from, to) => {
  if (!failed && from.endsWith('.new') && to.endsWith('secondary.auth.json')) {
    failed = true;
    throw Object.assign(new Error('Injected disk failure'), {code:'ENOSPC'});
  }
  return rename(from, to);
};
""")
                before = self.snapshot()
                fresh = {"version": 1, "profiles": {p: credential(p, age=0, remaining=5000) for p in module.PROFILES}}
                with patch.dict(os.environ, {"NODE_OPTIONS": "--import=" + str(injector)}):
                    with self.assertRaises(module.AuthError):
                        self.client.receive(fresh)
                self.assertEqual(self.snapshot(), before)

    def test_sender_strips_secrets_before_ssh_and_recovers_after_offline(self):
        source = self.home / "source"
        source.mkdir()
        for p in module.PROFILES:
            value = credential(p)
            value["tokens"]["refresh_token"] = "NEVER-SEND-REFRESH"
            value["OPENAI_API_KEY"] = "NEVER-SEND-API-KEY"
            (source / (p + ".auth.json")).write_text(json.dumps(value))
        def transport(args, **kwargs):
            self.assertNotIn("NEVER-SEND", kwargs["input"])
            self.assertNotIn("fake.", " ".join(args))
            receipt = self.client.receive(json.loads(kwargs["input"]))
            return subprocess.CompletedProcess(args, 0, json.dumps(receipt), "")
        real_run = subprocess.run
        def run(args, **kwargs):
            return transport(args, **kwargs) if args[0] == "ssh" else real_run(args, **kwargs)
        with patch.object(module.subprocess, "run", return_value=subprocess.CompletedProcess([], 255, "", "offline")):
            with self.assertRaises(module.AuthError):
                module.publish(source, "archbox", "/receiver", 3)
        self.assertFalse((self.codex / "auth.json").exists())
        with patch.object(module.subprocess, "run", side_effect=run):
            module.publish(source, "archbox", "/receiver", 3)
        self.assertTrue((self.codex / "auth.json").exists())

    def test_owner_operations_blocked_in_consumer_mode(self):
        self.client.receive(self.envelope)
        for args in (["save", "shared"], ["login", "secondary"], ["pi", "save", "shared"],
                     ["pi", "convert", "shared"], ["config", "set", "auto_switch.enabled", "true"]):
            with self.subTest(args=args), self.assertRaises(module.AuthError):
                self.client.profile_command(args)

    def test_manager_dispatch_does_not_enter_owner_flow(self):
        self.client.receive(self.envelope)
        result = subprocess.run(["bash", str(ROOT / "bin/codex-profile"), "use", "shared"],
                                env={**os.environ, "CODEX_HOME": str(self.codex)}, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.client.selection(), "shared")
        self.assertEqual(module.read_json(self.pi)["openai-codex"]["accountId"], "shared-id")

    def test_manager_honors_shared_home_and_store_overrides(self):
        custom = self.home / "custom-store"
        custom.mkdir()
        (custom / "consumer.json").write_text(json.dumps(self.config))
        overrides = {"CODEX_HOME": str(self.home / "unused"), "CODEX_SHARED_HOME": str(self.codex),
                     "CODEX_PROFILE_STORE": str(custom), "CODEX_AUTH_PROFILES_HOME": str(custom / "profiles"),
                     "CODEX_PROFILE_DEFAULT_FILE": str(custom / "default"),
                     "CODEX_CURRENT_PROFILE_FILE": str(custom / "current"),
                     "CODEX_PROFILE_CONSUMER_FILE": str(custom / "consumer")}
        with patch.dict(os.environ, overrides):
            client = module.Consumer()
            client.receive(self.envelope)
            result = subprocess.run(["bash", str(ROOT / "bin/codex-profile"), "use", "shared"],
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(client.selection(), "shared")
            self.assertEqual(module.read_json(self.codex / "auth.json")["tokens"]["account_id"], "shared-id")
        self.assertFalse((self.home / "unused").exists())


if __name__ == "__main__":
    unittest.main()
