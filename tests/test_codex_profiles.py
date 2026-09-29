"""Public contract tests for Codex profile publication; all credentials are fake."""
import datetime
import json
import os
from pathlib import Path
import shlex
import shutil
import subprocess
import sys
import tempfile
import time
import unittest

PROFILE = str(Path(__file__).resolve().parents[1] / "bin" / "codex-profile")

# Todo destino sensible se fija desde la fixture: un override heredado del host
# no debe poder desviar escrituras fuera del temporal en NINGÚN test.
SENSITIVE_ENV = ("CODEX_SHARED_HOME", "CODEX_HOME", "AUTH_FILE", "CODEX_PROFILE_STORE",
                 "CODEX_AUTH_PROFILES_HOME", "CODEX_PROFILE_LIMITS_HOME",
                 "CODEX_PROFILE_PROBE_ROOT", "CODEX_PROFILE_DEFAULT_FILE",
                 "CODEX_CURRENT_PROFILE_FILE", "CODEX_PROFILE_CONFIG_FILE",
                 "CODEX_LEGACY_PROFILE_ROOT", "PI_AUTH_FILE", "CODEX_BIN", "CODEX_LOGIN_BIN",
                 "CODEX_PROFILE_DEBUG", "PROFILE_LOCK_HELD")


def clean_env(**overrides):
    env = {key: value for key, value in os.environ.items() if key not in SENSITIVE_ENV}
    env.update(overrides)
    return env


class ProfilePublicationTest(unittest.TestCase):
    def test_save_rejects_live_account_mismatch_without_overwriting_profile(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex = home / "codex"
            accounts = codex / ".codex-profile" / "accounts"
            accounts.mkdir(parents=True)
            saved = accounts / "shared.auth.json"
            saved.write_text(json.dumps({"tokens": {"account_id": "fake-shared", "access_token": "old"}}))
            before = saved.read_bytes()
            (codex / "auth.json").write_text(json.dumps({"tokens": {
                "account_id": "fake-secondary", "access_token": "new"}}))
            env = clean_env(HOME=str(home), CODEX_SHARED_HOME=str(codex),
                            CODEX_PROFILE_STORE=str(codex / ".codex-profile"))
            result = subprocess.run([PROFILE, "save", "shared"], env=env,
                                    capture_output=True, text=True, timeout=8)
            self.assertNotEqual(result.returncode, 0)
            self.assertEqual(saved.read_bytes(), before)

    def test_saved_profile_is_complete_during_save(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex = home / "codex"
            accounts = codex / ".codex-profile" / "accounts"
            accounts.mkdir(parents=True)
            live = codex / "auth.json"
            saved = accounts / "shared.auth.json"
            old = {"tokens": {"access_token": "old", "account_id": "fake"}}
            new = {"tokens": {"access_token": "new" * 1000, "account_id": "fake"}}
            saved.write_text(json.dumps(old))
            live.write_text(json.dumps(new))
            bin_dir = home / "bin"
            bin_dir.mkdir()
            # Model a slow filesystem copy: freeze after the destination is half-written.
            cp = bin_dir / "cp"
            cp.write_text("""#!/usr/bin/env python3
import os, pathlib, shutil, sys, time
source, destination = map(pathlib.Path, sys.argv[1:])
if destination == pathlib.Path(os.environ['TEST_SAVED_PROFILE']):
    text = source.read_text()
    destination.write_text(text[:len(text)//2])
    pathlib.Path(os.environ['TEST_COPY_READY']).touch()
    deadline = time.monotonic() + 5
    while not pathlib.Path(os.environ['TEST_COPY_RELEASE']).exists() and time.monotonic() < deadline:
        time.sleep(0.01)
    destination.write_text(text)
else:
    shutil.copyfile(source, destination)
""")
            cp.chmod(0o700)
            ready, release = home / "copy-ready", home / "copy-release"
            env = clean_env(HOME=str(home), CODEX_SHARED_HOME=str(codex),
                            CODEX_PROFILE_STORE=str(codex / ".codex-profile"),
                            TEST_SAVED_PROFILE=str(saved), TEST_COPY_READY=str(ready),
                            TEST_COPY_RELEASE=str(release), PATH=f"{bin_dir}:{os.environ['PATH']}")
            proc = subprocess.Popen([PROFILE, "save", "shared"], env=env,
                                    stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
            try:
                deadline = time.monotonic() + 5
                while not ready.exists() and proc.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                partial = None
                if ready.exists():
                    try:
                        partial = json.loads(saved.read_text())
                    except json.JSONDecodeError:
                        partial = "invalid"
            finally:
                release.touch()
                stdout, stderr = proc.communicate(timeout=8)
            self.assertEqual(proc.returncode, 0, stderr)
            self.assertNotEqual(partial, "invalid", "a reader observed a partial saved profile")
            self.assertEqual(json.loads(saved.read_text()), new)
            self.assertEqual(saved.stat().st_mode & 0o777, 0o600)


    def test_unchanged_pi_snapshot_does_not_rewrite_live_auth(self):
        import base64

        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex = home / "codex"
            accounts = codex / ".codex-profile" / "accounts"
            accounts.mkdir(parents=True)
            payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 900000}).encode()).decode().rstrip("=")
            (accounts / "shared.auth.json").write_text(json.dumps({"tokens": {
                "access_token": f"fake.{payload}.sig", "refresh_token": "fake-refresh",
                "account_id": "fake-account"}}))
            live_pi = home / "pi" / "auth.json"
            env = clean_env(HOME=str(home), CODEX_SHARED_HOME=str(codex),
                            CODEX_PROFILE_STORE=str(codex / ".codex-profile"),
                            PI_AUTH_FILE=str(live_pi))
            for command in (("pi", "convert", "shared"), ("pi", "use", "shared")):
                result = subprocess.run([PROFILE, *command], env=env,
                                        capture_output=True, text=True, timeout=8)
                self.assertEqual(result.returncode, 0, result.stderr)
            saved_pi = accounts / "shared.pi-auth.json"
            os.utime(saved_pi, (1000000000, 1000000000))
            os.utime(live_pi, (1000000000, 1000000000))
            result = subprocess.run([PROFILE, "pi", "sync", "shared"], env=env,
                                    capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(saved_pi.stat().st_mtime_ns, 1000000000 * 10**9)
            self.assertEqual(live_pi.stat().st_mtime_ns, 1000000000 * 10**9)
            original_expiry = json.loads(saved_pi.read_text())["openai-codex"]["expires"]
            tampered = json.loads(saved_pi.read_text())
            tampered["openai-codex"]["expires"] += 86400000
            saved_pi.write_text(json.dumps(tampered))
            result = subprocess.run([PROFILE, "pi", "sync", "shared"], env=env,
                                    capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(saved_pi.read_text())["openai-codex"]["expires"], original_expiry)
            expired_payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) - 60}).encode()).decode().rstrip("=")
            expired = {"tokens": {"access_token": f"fake.{expired_payload}.expired",
                                  "refresh_token": "fake-refresh", "account_id": "fake-account"}}
            (accounts / "shared.auth.json").write_text(json.dumps(expired))
            result = subprocess.run([PROFILE, "pi", "convert", "shared"], env=env,
                                    capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            os.utime(saved_pi, (1000000000, 1000000000))
            result = subprocess.run([PROFILE, "pi", "sync", "shared"], env=env,
                                    capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(saved_pi.stat().st_mtime_ns, 1000000000 * 10**9)

    def test_pi_use_and_pi_sync_cannot_interleave(self):
        import base64

        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex = home / "codex"
            accounts = codex / ".codex-profile" / "accounts"
            accounts.mkdir(parents=True)
            payload = base64.urlsafe_b64encode(json.dumps({"exp": int(time.time()) + 900000}).encode()).decode().rstrip("=")
            def auth(label):
                return {"tokens": {"access_token": f"fake.{payload}.{label}",
                                   "refresh_token": f"fake-refresh-{label}",
                                   "account_id": f"fake-account-{label}"}}
            for profile in ("shared", "secondary"):
                (accounts / f"{profile}.auth.json").write_text(json.dumps(auth(profile)))
            live_pi = home / "pi" / "auth.json"
            env = clean_env(HOME=str(home), CODEX_SHARED_HOME=str(codex),
                            CODEX_PROFILE_STORE=str(codex / ".codex-profile"),
                            PI_AUTH_FILE=str(live_pi))
            for command in (("pi", "convert", "shared"), ("pi", "convert", "secondary"),
                            ("pi", "use", "shared")):
                result = subprocess.run([PROFILE, *command], env=env,
                                        capture_output=True, text=True, timeout=8)
                self.assertEqual(result.returncode, 0, result.stderr)
            rotated = auth("secondary")
            rotated["tokens"]["access_token"] = f"fake.{payload}.secondary-rotated"
            (accounts / "secondary.auth.json").write_text(json.dumps(rotated))
            bin_dir = home / "bin"
            bin_dir.mkdir()
            ready, release = home / "pi-use-ready", home / "pi-use-release"
            fake_python = bin_dir / "python3"
            fake_python.write_text("""#!/bin/bash
if [[ "$1" == '-' ]]; then
    body="$(mktemp)"
    cat > "$body"
    if [[ "$2" == "$TEST_PI_LIVE" && "$3" == "$TEST_SECONDARY_PI" ]] &&
       grep -q 'live\\[provider\\] = block' "$body"; then
        touch "$TEST_USE_READY"
        end=$((SECONDS + 6))
        while [[ ! -e "$TEST_USE_RELEASE" && $SECONDS -lt $end ]]; do sleep 0.01; done
    fi
    /usr/bin/python3 "$body" "${@:2}"
    rc=$?
    rm -f "$body"
    exit "$rc"
fi
exec /usr/bin/python3 "$@"
""")
            fake_python.chmod(0o700)
            env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", TEST_PI_LIVE=str(live_pi),
                       TEST_SECONDARY_PI=str(accounts / "secondary.pi-auth.json"),
                       TEST_USE_READY=str(ready), TEST_USE_RELEASE=str(release))
            processes = []
            try:
                use = subprocess.Popen([PROFILE, "pi", "use", "secondary"], env=env,
                                       stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(use)
                deadline = time.monotonic() + 5
                while not ready.exists() and use.poll() is None and time.monotonic() < deadline:
                    time.sleep(0.01)
                self.assertTrue(ready.exists(), "pi use did not reach live Pi auth publication")
                sync = subprocess.Popen([PROFILE, "pi", "sync", "secondary"], env=env,
                                        stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
                processes.append(sync)
                time.sleep(0.3)
                self.assertIsNone(sync.poll(), "pi sync ran inside pi use transaction")
            finally:
                release.touch()
                for proc in processes:
                    out, err = proc.communicate(timeout=10)
                    self.assertEqual(proc.returncode, 0, err)
            live_codex = json.loads((codex / "auth.json").read_text())['tokens']
            live_pi_block = json.loads(live_pi.read_text())['openai-codex']
            self.assertEqual(live_codex['account_id'], live_pi_block['accountId'])








class ProfileActivationTest(unittest.TestCase):
    """'use' must keep Codex and Pi in sync; every credential in these fixtures is fake."""

    @staticmethod
    def codex_auth(account, label):
        return {"tokens": {"access_token": f"fake-access-{label}",
                           "refresh_token": f"fake-refresh-{label}",
                           "account_id": account}}

    @staticmethod
    def pi_auth(account, label):
        return {"openai-codex": {"type": "oauth", "access": f"fake-access-{label}",
                                 "refresh": f"fake-refresh-{label}",
                                 "expires": 4102444800000, "accountId": account}}

    def build_store(self, home):
        codex = home / "codex"
        store = codex / ".codex-profile"
        accounts = store / "accounts"
        accounts.mkdir(parents=True)
        for profile, account in (("shared", "fake-shared"), ("secondary", "fake-secondary")):
            (accounts / f"{profile}.auth.json").write_text(json.dumps(self.codex_auth(account, profile)))
            (accounts / f"{profile}.pi-auth.json").write_text(json.dumps(self.pi_auth(account, profile)))
        live_codex = codex / "auth.json"
        live_codex.write_text(json.dumps(self.codex_auth("fake-shared", "shared")))
        live_pi = home / "pi" / "auth.json"
        live_pi.parent.mkdir(parents=True)
        live_pi.write_text(json.dumps(self.pi_auth("fake-shared", "shared")))
        (store / ".default-profile").write_text("shared\n")
        (store / ".current-profile").write_text("shared\n")
        return codex, store, accounts, live_codex, live_pi

    def env_for(self, home, codex, store, live_pi):
        bin_dir = home / "bin"
        bin_dir.mkdir(exist_ok=True)
        fake_codex = bin_dir / "codex-fake"
        fake_codex.write_text("#!/bin/sh\n[ \"$1 $2\" = 'login status' ] || exit 9\necho 'Logged in using ChatGPT'\n")
        fake_codex.chmod(0o700)
        # Todo destino sensible se fija al temporal: un override heredado del host
        # no debe poder desviar escrituras fuera de la fixture.
        return clean_env(HOME=str(home), CODEX_SHARED_HOME=str(codex), CODEX_HOME=str(codex),
                         AUTH_FILE=str(codex / "auth.json"), CODEX_PROFILE_STORE=str(store),
                         CODEX_AUTH_PROFILES_HOME=str(store / "accounts"),
                         CODEX_PROFILE_LIMITS_HOME=str(store / "limits"),
                         CODEX_PROFILE_PROBE_ROOT=str(store / "probes"),
                         CODEX_PROFILE_DEFAULT_FILE=str(store / ".default-profile"),
                         CODEX_CURRENT_PROFILE_FILE=str(store / ".current-profile"),
                         CODEX_PROFILE_CONFIG_FILE=str(store / "config.toml"),
                         CODEX_LEGACY_PROFILE_ROOT=str(home / "legacy-profiles"),
                         PI_AUTH_FILE=str(live_pi), CODEX_BIN=str(fake_codex))

    def test_use_activates_codex_and_pi_and_records_default(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.env_for(home, codex, store, live_pi)
            result = subprocess.run([PROFILE, "use", "secondary"], env=env,
                                    capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"],
                             "fake-secondary", "live Codex auth was not switched")
            self.assertEqual(json.loads(live_pi.read_text())["openai-codex"]["accountId"],
                             "fake-secondary", "live Pi auth was not switched")
            self.assertEqual((store / ".default-profile").read_text().strip(), "secondary")
            self.assertEqual((store / ".current-profile").read_text().strip(), "secondary")
            self.assertIn("secondary", result.stdout)

    def test_use_is_idempotent_and_pi_use_still_works(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.env_for(home, codex, store, live_pi)
            for command in (("use", "secondary"), ("use", "secondary"),
                            ("pi", "use", "secondary")):
                result = subprocess.run([PROFILE, *command], env=env,
                                        capture_output=True, text=True, timeout=8)
                self.assertEqual(result.returncode, 0, f"{command}: {result.stderr}")
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"], "fake-secondary")
            self.assertEqual(json.loads(live_pi.read_text())["openai-codex"]["accountId"], "fake-secondary")
            self.assertEqual((store / ".default-profile").read_text().strip(), "secondary")

    def test_use_without_pi_snapshot_builds_it_from_codex_and_switches_both(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            (accounts / "secondary.pi-auth.json").unlink()
            env = self.env_for(home, codex, store, live_pi)
            result = subprocess.run([PROFILE, "use", "secondary"], env=env,
                                    capture_output=True, text=True, timeout=8)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"], "fake-secondary")
            self.assertEqual(json.loads(live_pi.read_text())["openai-codex"]["accountId"],
                             "fake-secondary", "Pi was left on the previous account")
            snapshot = json.loads((accounts / "secondary.pi-auth.json").read_text())["openai-codex"]
            self.assertEqual(snapshot["accountId"], "fake-secondary")
            self.assertIn("Pi auth snapshot", result.stdout)

    def test_use_unknown_profile_fails_without_partial_state(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.env_for(home, codex, store, live_pi)
            result = subprocess.run([PROFILE, "use", "ghost"], env=env,
                                    capture_output=True, text=True, timeout=8)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("login ghost", result.stdout + result.stderr)
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"], "fake-shared")
            self.assertEqual(json.loads(live_pi.read_text())["openai-codex"]["accountId"], "fake-shared")
            self.assertEqual((store / ".default-profile").read_text().strip(), "shared")

    def test_status_reports_both_identities_and_warns_on_stale_pi(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            # Codex is on secondary, but Pi was left behind on shared.
            live_codex.write_text(json.dumps(self.codex_auth("fake-secondary", "secondary")))
            (store / ".current-profile").write_text("secondary\n")
            (store / ".default-profile").write_text("secondary\n")
            env = self.env_for(home, codex, store, live_pi)
            result = subprocess.run([PROFILE, "status"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("default=secondary", output)
            self.assertIn("live codex=fake-secondary", output)
            self.assertIn("live pi=fake-shared", output)
            self.assertIn("shared: codex=fake-shared pi=fake-shared", output)
            self.assertIn("secondary: codex=fake-secondary pi=fake-secondary", output)
            self.assertIn("codex-profile use secondary", output)
            self.assertIn("does not match", output)

    def test_status_has_no_warning_when_codex_and_pi_match(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            live_codex.write_text(json.dumps(self.codex_auth("fake-secondary", "secondary")))
            live_pi.write_text(json.dumps(self.pi_auth("fake-secondary", "secondary")))
            (store / ".current-profile").write_text("secondary\n")
            env = self.env_for(home, codex, store, live_pi)
            result = subprocess.run([PROFILE, "status"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("live codex=fake-secondary", output)
            self.assertIn("live pi=fake-secondary", output)
            self.assertNotIn("does not match", output)

    def test_use_rejects_pi_snapshot_saved_from_another_account(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            # Snapshot mal etiquetado: el Pi de 'secondary' guarda la cuenta de shared.
            (accounts / "secondary.pi-auth.json").write_text(
                json.dumps(self.pi_auth("fake-shared", "shared")))
            env = self.env_for(home, codex, store, live_pi)
            result = subprocess.run([PROFILE, "use", "secondary"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("pi convert", output)
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"], "fake-shared")
            self.assertEqual(json.loads(live_pi.read_text())["openai-codex"]["accountId"], "fake-shared")
            self.assertEqual((store / ".current-profile").read_text().strip(), "shared")
            self.assertEqual((store / ".default-profile").read_text().strip(), "shared")

    def test_use_fails_cleanly_when_live_pi_auth_is_not_json(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            broken = "{not json"
            live_pi.write_text(broken)
            env = self.env_for(home, codex, store, live_pi)
            result = subprocess.run([PROFILE, "use", "secondary"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("not valid JSON", output)
            self.assertEqual(live_pi.read_text(), broken)
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"], "fake-shared")
            self.assertEqual((store / ".current-profile").read_text().strip(), "shared")
            self.assertEqual((store / ".default-profile").read_text().strip(), "shared")

    def test_use_restores_previous_state_when_pi_publication_fails(self):
        # Variante por permisos: solo es concluyente si el proceso no es root; la
        # variante determinista (shim de python3) es la que no depende de ello.
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.env_for(home, codex, store, live_pi)
            live_pi.parent.chmod(0o500)  # el temporal de Pi no se puede crear
            try:
                result = subprocess.run([PROFILE, "use", "secondary"], env=env,
                                        capture_output=True, text=True, timeout=8)
            finally:
                live_pi.parent.chmod(0o700)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("previous auth state was restored", output)
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"],
                             "fake-shared", "Codex auth was left on the new profile")
            self.assertEqual(json.loads(live_pi.read_text())["openai-codex"]["accountId"],
                             "fake-shared", "Pi auth was left on the new profile")
            self.assertEqual((store / ".current-profile").read_text().strip(), "shared")
            self.assertEqual((store / ".default-profile").read_text().strip(), "shared")

    def test_status_warns_on_live_mismatch_even_with_stale_current_profile(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            # Tokens rotados: misma cuenta que secondary pero sin coincidencia byte a
            # byte, y el .current-profile registrado es obsoleto ('shared').
            live_codex.write_text(json.dumps(self.codex_auth("fake-secondary", "rotated")))
            (store / ".current-profile").write_text("shared\n")
            env = self.env_for(home, codex, store, live_pi)
            result = subprocess.run([PROFILE, "status"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("live codex=fake-secondary", output)
            self.assertIn("live pi=fake-shared", output)
            self.assertIn("does not match", output)
            self.assertNotIn("use shared", output)  # perfil no confirmado: sin recomendación

    def test_use_restores_all_states_when_pi_publication_fails_deterministically(self):
        # Variante sin depender de permisos ni de ser root: un shim de python3 falla
        # SOLO la publicación del auth Pi vivo (tres argumentos), no su validación
        # previa ni el resto de comprobaciones de identidad.
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            bin_dir = home / "bin"
            bin_dir.mkdir(exist_ok=True)
            shim = bin_dir / "python3"
            shim.write_text("""#!/bin/bash
if [[ "$1" == '-' && "$2" == "$TEST_PI_LIVE" && -n "${3:-}" ]]; then
    exit 1
fi
exec /usr/bin/python3 "$@"
""")
            shim.chmod(0o700)
            env = self.env_for(home, codex, store, live_pi)
            env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", TEST_PI_LIVE=str(live_pi))
            result = subprocess.run([PROFILE, "use", "secondary"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("previous auth state was restored", output)
            self.assertNotIn("could NOT be fully restored", output)
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"], "fake-shared")
            self.assertEqual(json.loads(live_pi.read_text())["openai-codex"]["accountId"], "fake-shared")
            self.assertEqual((store / ".current-profile").read_text().strip(), "shared")
            self.assertEqual((store / ".default-profile").read_text().strip(), "shared")

    def test_use_fails_transactionally_when_codex_copy_fails(self):
        # Un fallo interno de la publicación de Codex no puede acabar en éxito
        # falso: el shim de cp falla solo la primera copia dirigida al hogar vivo
        # de Codex (la de restore_profile_auth), y la restauración posterior sí
        # puede completarse.
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            bin_dir = home / "bin"
            bin_dir.mkdir(exist_ok=True)
            cp = bin_dir / "cp"
            cp.write_text("""#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
target = Path(args[-1]) if args else Path('')
marker = Path(os.environ['TEST_CP_FAIL_MARKER'])
if os.path.dirname(str(target)) == os.environ['TEST_CP_FAIL_DIR'] and not marker.exists():
    marker.touch()
    sys.exit(1)
os.execv('/usr/bin/cp', ['cp', *args])
""")
            cp.chmod(0o700)
            env = self.env_for(home, codex, store, live_pi)
            env.update(PATH=f"{bin_dir}:{os.environ['PATH']}",
                       TEST_CP_FAIL_DIR=str(codex),
                       TEST_CP_FAIL_MARKER=str(home / "cp-failed-once"))
            result = subprocess.run([PROFILE, "use", "secondary"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, "a failed Codex copy must not report success")
            self.assertIn("previous auth state was restored", output)
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"], "fake-shared")
            self.assertEqual(json.loads(live_pi.read_text())["openai-codex"]["accountId"], "fake-shared")
            self.assertEqual((store / ".current-profile").read_text().strip(), "shared")
            self.assertEqual((store / ".default-profile").read_text().strip(), "shared")

    def test_use_rejects_snapshots_without_readable_identity(self):
        cases = (
            ("Codex snapshot is not JSON", "secondary.auth.json", "{not json",
             "no readable Codex identity", "login secondary"),
            ("Pi snapshot has no accountId", "secondary.pi-auth.json",
             json.dumps({"openai-codex": {"type": "oauth", "access": "fake-access-secondary",
                                          "refresh": "fake-refresh-secondary",
                                          "expires": 4102444800000}}),
             "no readable Pi identity", "pi convert secondary"),
        )
        for label, filename, payload, message, repair in cases:
            with self.subTest(label=label), tempfile.TemporaryDirectory() as root:
                home = Path(root)
                codex, store, accounts, live_codex, live_pi = self.build_store(home)
                (accounts / filename).write_text(payload)
                env = self.env_for(home, codex, store, live_pi)
                result = subprocess.run([PROFILE, "use", "secondary"], env=env,
                                        capture_output=True, text=True, timeout=8)
                output = result.stdout + result.stderr
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, output)
                self.assertIn(repair, output)
                self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"],
                                 "fake-shared", "live Codex auth changed despite the rejection")
                self.assertEqual(json.loads(live_pi.read_text())["openai-codex"]["accountId"],
                                 "fake-shared", "live Pi auth changed despite the rejection")
                self.assertEqual((store / ".current-profile").read_text().strip(), "shared")
                self.assertEqual((store / ".default-profile").read_text().strip(), "shared")

    def test_use_fails_transactionally_when_default_cannot_be_written(self):
        # El perfil por defecto entra en la transacción: si su escritura falla, el
        # auth de Codex, el de Pi y el perfil actual vuelven a su estado previo.
        # El destino apunta a un padre inexistente: falla igual incluso como root.
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            unwritable = home / "absent-dir" / ".default-profile"
            env = self.env_for(home, codex, store, live_pi)
            env.update(CODEX_PROFILE_DEFAULT_FILE=str(unwritable))
            result = subprocess.run([PROFILE, "use", "secondary"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("previous auth state was restored", output)
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"], "fake-shared")
            self.assertEqual(json.loads(live_pi.read_text())["openai-codex"]["accountId"], "fake-shared")
            self.assertEqual((store / ".current-profile").read_text().strip(), "shared")
            self.assertEqual((store / ".default-profile").read_text().strip(), "shared")
            self.assertFalse(unwritable.exists())

    def test_status_survives_failing_codex_login_status(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            live_codex.write_text(json.dumps(self.codex_auth("fake-secondary", "secondary")))
            (store / ".current-profile").write_text("secondary\n")
            broken = home / "bin" / "codex-broken"
            broken.parent.mkdir(exist_ok=True)
            broken.write_text("#!/bin/sh\nexit 1\n")
            broken.chmod(0o700)
            env = self.env_for(home, codex, store, live_pi)
            env["CODEX_BIN"] = str(broken)
            result = subprocess.run([PROFILE, "status"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("login status' failed", output)
            self.assertIn("live codex=fake-secondary", output)
            self.assertIn("live pi=fake-shared", output)
            self.assertIn("does not match", output)

    def test_use_aborts_untouched_when_the_switch_backup_cannot_be_taken(self):
        # La captura previa es completa o la activación no empieza: si el respaldo
        # falla, ningún estado vivo se toca y no queda basura en el store.
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            bin_dir = home / "bin"
            bin_dir.mkdir(exist_ok=True)
            cp = bin_dir / "cp"
            cp.write_text("""#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
target = Path(args[-1]) if args else Path('')
if str(target).startswith(os.environ['TEST_CP_BLOCK_PREFIX']):
    sys.exit(1)
os.execv('/usr/bin/cp', ['cp', *args])
""")
            cp.chmod(0o700)
            env = self.env_for(home, codex, store, live_pi)
            env.update(PATH=f"{bin_dir}:{os.environ['PATH']}",
                       TEST_CP_BLOCK_PREFIX=str(store / "switch-state."))
            result = subprocess.run([PROFILE, "use", "secondary"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("Could not back up", output)
            self.assertIn("no state was changed", output)
            self.assertNotIn("the previous auth state was restored", output)
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"], "fake-shared")
            self.assertEqual(json.loads(live_pi.read_text())["openai-codex"]["accountId"], "fake-shared")
            self.assertEqual((store / ".current-profile").read_text().strip(), "shared")
            self.assertEqual((store / ".default-profile").read_text().strip(), "shared")
            self.assertEqual(list(store.glob("switch-state.*")), [], "leftover backup directory")

    def test_use_names_the_state_it_could_not_restore(self):
        # Si la restauración falla, no se anuncia una restauración global: se nombra
        # exactamente el estado que quedó sin restaurar Y el respaldo se conserva con
        # su ruta, porque una copia fallida pudo dejar un estado vivo truncado.
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            bin_dir = home / "bin"
            bin_dir.mkdir(exist_ok=True)
            py = bin_dir / "python3"
            py.write_text("""#!/bin/bash
if [[ "$1" == '-' && "$2" == "$TEST_PI_LIVE" && -n "${3:-}" ]]; then
    exit 1
fi
exec /usr/bin/python3 "$@"
""")
            py.chmod(0o700)
            cp = bin_dir / "cp"
            cp.write_text("""#!/usr/bin/env python3
import os, sys
from pathlib import Path
args = sys.argv[1:]
target = Path(args[-1]) if args else Path('')
if os.path.dirname(str(target)) == os.environ['TEST_CP_BLOCK_DIR']:
    sys.exit(1)
os.execv('/usr/bin/cp', ['cp', *args])
""")
            cp.chmod(0o700)
            env = self.env_for(home, codex, store, live_pi)
            env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", TEST_PI_LIVE=str(live_pi),
                       TEST_CP_BLOCK_DIR=str(live_pi.parent))
            result = subprocess.run([PROFILE, "use", "secondary"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0)
            self.assertIn(f"Could not restore: {live_pi}", output)
            self.assertIn("could NOT be fully restored", output)
            self.assertIn("keep this backup until repaired", output)
            self.assertNotIn("the previous auth state was restored", output)
            # Codex y los marcadores sí volvieron a su estado previo.
            self.assertEqual(json.loads(live_codex.read_text())["tokens"]["account_id"], "fake-shared")
            self.assertEqual((store / ".current-profile").read_text().strip(), "shared")
            self.assertEqual((store / ".default-profile").read_text().strip(), "shared")
            # El respaldo queda en disco con los cuatro estados: es la vía de
            # recuperación mientras el auth Pi vivo siga sin restaurar.
            backups = list(store.glob("switch-state.*"))
            self.assertEqual(len(backups), 1, "the backup must be kept when the restore fails")
            self.assertIn(str(backups[0]), output, "the kept backup path must be printed")
            for name in ("codex.auth.json", "pi.auth.json", "current", "default"):
                self.assertTrue((backups[0] / name).is_file(), f"kept backup is missing {name}")

    def test_pi_sync_reports_snapshot_write_failure_without_success_note(self):
        # 'pi sync' se invoca dentro de listas '&&' (set -e suprimido): un fallo al
        # escribir el snapshot no puede acabar en «Updated…» con rc 0. El shim falla
        # solo la escritura dirigida a un *.pi-auth.json.
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            (accounts / "secondary.pi-auth.json").unlink()
            bin_dir = home / "bin"
            bin_dir.mkdir(exist_ok=True)
            shim = bin_dir / "python3"
            shim.write_text("""#!/bin/bash
for arg in "$@"; do
    case "$arg" in
      *.pi-auth.json) exit 1 ;;
    esac
done
exec /usr/bin/python3 "$@"
""")
            shim.chmod(0o700)
            env = self.env_for(home, codex, store, live_pi)
            env["PATH"] = f"{bin_dir}:{os.environ['PATH']}"
            before = live_pi.read_bytes()
            result = subprocess.run([PROFILE, "pi", "sync", "secondary"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, "a failed snapshot write must not report success")
            self.assertNotIn("Updated", output)
            self.assertIn("Could not write the Pi auth snapshot", output)
            self.assertFalse((accounts / "secondary.pi-auth.json").exists())
            self.assertEqual(live_pi.read_bytes(), before, "live Pi auth changed despite the failure")

    def test_pi_sync_reports_live_publication_failure_without_success_note(self):
        # Segunda operación interna: el snapshot se escribe, pero publicarlo en el
        # auth Pi vivo falla -> 'pi sync' sale no-cero y sin «Updated».
        # Ojo: la comprobación de coincidencia usa los MISMOS argumentos que la
        # publicación, así que el shim distingue por el cuerpo del script (como el
        # test de no-intercalado): solo falla la publicación real.
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            live_pi.write_text(json.dumps(self.pi_auth("fake-secondary", "secondary")))
            bin_dir = home / "bin"
            bin_dir.mkdir(exist_ok=True)
            shim = bin_dir / "python3"
            # Shebang absoluto: 'env python3' se resolvería a este mismo shim (recursión).
            shim.write_text("""#!/usr/bin/python3
import os, subprocess, sys
args = sys.argv[1:]
if args and args[0] == "-":
    body = sys.stdin.read()
    if len(args) >= 2 and args[1] == os.environ["TEST_PI_LIVE"] and "live[provider] = block" in body:
        sys.exit(1)
    sys.exit(subprocess.run(["/usr/bin/python3", "-", *args[1:]], input=body, text=True).returncode)
os.execv("/usr/bin/python3", ["python3", *args])
""")
            shim.chmod(0o700)
            env = self.env_for(home, codex, store, live_pi)
            env.update(PATH=f"{bin_dir}:{os.environ['PATH']}", TEST_PI_LIVE=str(live_pi))
            before = live_pi.read_bytes()
            result = subprocess.run([PROFILE, "pi", "sync", "secondary"], env=env,
                                    capture_output=True, text=True, timeout=8)
            output = result.stdout + result.stderr
            self.assertNotEqual(result.returncode, 0, "a failed live publication must not report success")
            self.assertNotIn("Updated", output)
            self.assertIn("Could not publish the Pi auth snapshot", output)
            self.assertEqual(live_pi.read_bytes(), before, "live Pi auth changed despite the failure")


class ManagedDaemonInvalidationTest(unittest.TestCase):
    """El daemon app-server nace con la cuenta del auth vivo y no la relee.

    Tras un cambio de perfil hay que invalidarlo, o la TUI sigue sirviendo la cuenta
    anterior (incidente 29-sep: auth vivo en secondary y /status con la ventana de
    shared). Estas pruebas forjan un daemon real: un proceso con 'app-server' en su
    cmdline y un daemon.pid con el startTicks auténtico de /proc/<pid>/stat.
    """

    # Fixtures de activación reutilizadas (store temporal, auths falsas) sin duplicarlas.
    codex_auth = staticmethod(ProfileActivationTest.codex_auth)
    pi_auth = staticmethod(ProfileActivationTest.pi_auth)
    build_store = ProfileActivationTest.build_store
    env_for = ProfileActivationTest.env_for

    @staticmethod
    def stop_process(proc):
        if proc.poll() is None:
            proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass

    def write_daemon_pid(self, codex, payload):
        daemon_dir = codex / "app-server-daemon"
        daemon_dir.mkdir(parents=True, exist_ok=True)
        (daemon_dir / "daemon.pid").write_text(
            payload if isinstance(payload, str) else json.dumps(payload))

    @staticmethod
    def real_boot_id():
        return Path("/proc/sys/kernel/random/boot_id").read_text().strip()

    @staticmethod
    def process_ticks(pid):
        return int(Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[19])

    @staticmethod
    def set_auth_mtime(auth_file, epoch):
        """Fija el mtime del auth: la frescura del daemon se compara contra esa fecha."""
        os.utime(auth_file, (epoch, epoch))

    def fake_daemon(self, codex, born_offset_s=-3600, ticks_delta=0,
                    start_ticks=True, boot_id=None, ignore_term=False):
        """Arranca un proceso real que parece el app-server y publica su daemon.pid.

        Devuelve (proceso, born_epoch) con la fecha de nacimiento truncada a
        segundos: es la que el JSON puede expresar y la que fija el mtime del auth.
        """
        if ignore_term:
            script = Path(tempfile.mkdtemp(prefix="fake-daemon-")) / "app-server-fake.sh"
            self.addCleanup(shutil.rmtree, script.parent, ignore_errors=True)
            script.write_text('#!/usr/bin/env bash\ntrap "" TERM\nwhile :; do sleep 1; done\n')
            script.chmod(0o700)
            command = ('exec -a "codex app-server --listen unix:// --managed-daemon" '
                       + shlex.quote(str(script)))
        else:
            command = 'exec -a "codex app-server --listen unix:// --managed-daemon" sleep 300'
        proc = subprocess.Popen(["bash", "-c", command],
                                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_process, proc)
        born = datetime.datetime.fromtimestamp(time.time() + born_offset_s).replace(microsecond=0)
        identity = {"bootId": self.real_boot_id() if boot_id is None else boot_id}
        if start_ticks:
            identity["startTicks"] = self.process_ticks(proc.pid) + ticks_delta
        self.write_daemon_pid(codex, {
            "pid": proc.pid,
            "processStartTime": born.strftime("%a %b %d %H:%M:%S %Y"),
            "processIdentity": identity,
        })
        return proc, born.timestamp()

    def sudo(self, *args):
        return subprocess.run(["sudo", "-n", *args], capture_output=True, text=True)

    @staticmethod
    def cgroup_pids(cgroup):
        try:
            text = Path(cgroup, "cgroup.procs").read_text()
        except OSError:
            return []
        return [int(pid) for pid in text.split()]

    def frozen_survivor_daemon(self, codex, born_offset_s=-3600):
        """Daemon falso CONGELADO por cgroup v2: sobrevive a TERM y KILL porque las
        señales quedan pendientes hasta descongelar. Es el caso real «confirmado
        obsoleto y superviviente» sin depender de otro usuario (un operador con
        CAP_KILL señaliza incluso procesos root). Devuelve el pid o None si el
        entorno no permite crear/congelar el cgroup.
        """
        # Cgroup mutation requires explicit consent, even when sudo is available.
        if os.environ.get("CODEX_PROFILE_TEST_ROOT_CGROUP") != "1":
            return None
        # Nombre único por ejecución: el PID solo puede reutilizarse entre pruebas.
        cgroup = f"/sys/fs/cgroup/pi-freeze-test-{os.getpid()}-{time.time_ns()}"
        if self.sudo("mkdir", "-p", cgroup).returncode != 0:
            return None
        launcher = subprocess.Popen(
            ["sudo", "-n", "bash", "-c",
             f"echo $$ > {cgroup}/cgroup.procs; "
             'exec -a "codex app-server --listen unix:// --managed-daemon" sleep 300'],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(self.stop_process, launcher)

        def cleanup():
            # Descongela, vacía y espera a que el cgroup quede VACÍO antes de
            # retirarlo: un rmdir inmediato puede fallar si un proceso aún no salió.
            self.sudo("sh", "-c", f"echo 0 > {cgroup}/cgroup.freeze")
            for _ in range(50):
                pids = self.cgroup_pids(cgroup)
                if not pids:
                    break
                for pid in pids:
                    self.sudo("kill", "-KILL", str(pid))
                time.sleep(0.1)
            removed = None
            for _ in range(50):
                removed = self.sudo("rmdir", cgroup)
                if removed.returncode == 0:
                    return
                time.sleep(0.1)
            print(f"warning: no se pudo retirar el cgroup de prueba {cgroup}: "
                  f"{removed.stderr.strip()}", file=sys.stderr)

        self.addCleanup(cleanup)
        pid = None
        for _ in range(50):
            pids = self.cgroup_pids(cgroup)
            if pids:
                pid = pids[0]
                break
            time.sleep(0.1)
        if pid is None or self.sudo("sh", "-c", f"echo 1 > {cgroup}/cgroup.freeze").returncode != 0:
            return None
        born = datetime.datetime.fromtimestamp(time.time() + born_offset_s).replace(microsecond=0)
        self.write_daemon_pid(codex, {
            "pid": pid,
            "processStartTime": born.strftime("%a %b %d %H:%M:%S %Y"),
            "processIdentity": {"bootId": self.real_boot_id(),
                                "startTicks": self.process_ticks(pid)},
        })
        return pid

    def quiet_codex_env(self, home, codex, store, live_pi):
        env = self.env_for(home, codex, store, live_pi)
        fake = home / "bin" / "codex-quiet"
        fake.write_text("#!/bin/sh\nexit 0\n")
        fake.chmod(0o700)
        env["CODEX_BIN"] = str(fake)
        fake_pi = home / "bin" / "pi-quiet"
        fake_pi.write_text("#!/bin/sh\nexit 0\n")
        fake_pi.chmod(0o700)
        env["PI_BIN"] = str(fake_pi)
        return env

    def marker_env(self, home, codex, store, live_pi, marker):
        """Como quiet_codex_env, pero los shims anotan que el cliente se ejecutó.

        Permite distinguir «la guarda bloqueó la apertura» de «se abrió y el shim
        salió 0», que con un shim mudo es indistinguible por el código de salida.
        """
        env = self.quiet_codex_env(home, codex, store, live_pi)
        for name, key in (("codex-marker", "CODEX_BIN"), ("pi-marker", "PI_BIN")):
            shim = home / "bin" / name
            shim.write_text(f'#!/bin/sh\ntouch "{marker}"\nexit 0\n')
            shim.chmod(0o700)
            env[key] = str(shim)
        return env

    def run_profile(self, env, *args):
        return subprocess.run([PROFILE, *args], env=env, capture_output=True, text=True, timeout=15)

    def test_run_invalidates_a_daemon_born_before_the_live_auth(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(daemon.poll(),
                                 "el daemon anterior siguió vivo: la TUI serviría la cuenta previa")

    def test_run_keeps_a_daemon_born_strictly_after_the_live_auth(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            # El auth vivo YA es secondary: el switch no copia y el mtime que fijamos
            # es el que decide la frescura (si copiara, sería 'ahora' y el daemon
            # quedaría obsoleto de verdad).
            live_codex.write_bytes((accounts / "secondary.auth.json").read_bytes())
            (store / ".current-profile").write_text("secondary\n")
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, born = self.fake_daemon(codex, born_offset_s=-3600)
            self.set_auth_mtime(live_codex, born - 1.0)  # el auth es anterior al daemon
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(daemon.poll(), "se reinició un daemon que ya servía la cuenta actual")

    def test_run_invalidates_a_daemon_born_in_the_same_second_as_the_live_auth(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            live_codex.write_bytes((accounts / "secondary.auth.json").read_bytes())
            (store / ".current-profile").write_text("secondary\n")
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, born = self.fake_daemon(codex, born_offset_s=-3600)
            # La fecha del daemon solo tiene segundos: el empate es ambiguo y la
            # política es invalidar (nada de gracia de frescura).
            self.set_auth_mtime(live_codex, born)
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(daemon.poll(),
                                 "un empate de frescura se trató como daemon al día")

    def test_tui_open_refuses_an_app_server_process_whose_ticks_do_not_match(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "tui-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600, ticks_delta=1)
            result = self.run_profile(env, "run", "secondary")
            self.assertNotEqual(result.returncode, 0,
                                "una identidad no verificable no puede abrir la TUI")
            self.assertIn("could not verify the app-server daemon identity", result.stderr)
            self.assertIn("CODEX_PROFILE_ALLOW_STALE_DAEMON", result.stderr)
            self.assertFalse(marker.exists(), "la TUI se abrió pese a la identidad no verificada")
            self.assertIsNone(daemon.poll(), "no se señaliza a un proceso cuya identidad no se verificó")

    def test_run_without_a_daemon_pid_file_still_runs(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_run_to_the_profile_already_live_still_invalidates_the_old_daemon(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            # Caso observado: el auth vivo YA es secondary, así que switch_profile no
            # copia nada; el daemon viejo (de shared) debe caer igualmente.
            live_codex.write_bytes((accounts / "secondary.auth.json").read_bytes())
            (store / ".current-profile").write_text("secondary\n")
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(daemon.poll(),
                                 "el atajo sin copia dejó vivo el daemon de la cuenta anterior")

    def test_failed_switch_does_not_invalidate_the_daemon(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            result = self.run_profile(env, "run", "ghost")
            self.assertNotEqual(result.returncode, 0)
            self.assertIsNone(daemon.poll(), "se invalidó un daemon tras un switch fallido")

    def test_tui_open_refuses_a_corrupt_daemon_pid_file(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "tui-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            # Con el pid_file ilegible la identidad no es verificable: relajarse
            # exige el escape explícito, no vale el silencio.
            self.write_daemon_pid(codex, "not json at all")
            result = self.run_profile(env, "run", "secondary")
            self.assertNotEqual(result.returncode, 0,
                                "un pid_file corrupto abrió la TUI en silencio")
            self.assertIn("could not verify the app-server daemon identity", result.stderr)
            self.assertIn("CODEX_PROFILE_ALLOW_STALE_DAEMON", result.stderr)
            self.assertFalse(marker.exists(), "la TUI se abrió con un pid_file ilegible")

    def test_run_proceeds_when_the_daemon_pid_points_at_a_dead_process(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "tui-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            self.write_daemon_pid(codex, {"pid": 999999,
                                          "processStartTime": "Tue Jan  1 00:00:00 2030",
                                          "processIdentity": {"startTicks": 1}})
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists(), "la ausencia comprobada del daemon no debe bloquear")

    def test_tui_open_refuses_a_daemon_without_start_ticks(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "tui-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600, start_ticks=False)
            result = self.run_profile(env, "run", "secondary")
            self.assertNotEqual(result.returncode, 0,
                                "sin startTicks la identidad no es verificable y la TUI no se abre")
            self.assertIn("could not verify the app-server daemon identity", result.stderr)
            self.assertIn("CODEX_PROFILE_ALLOW_STALE_DAEMON", result.stderr)
            self.assertFalse(marker.exists(), "la TUI se abrió con identidad no verificable")
            self.assertIsNone(daemon.poll(),
                              "sin startTicks la identidad no es verificable: no se señaliza")

    def test_tui_open_refuses_a_daemon_from_another_boot(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "tui-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600,
                                         boot_id="00000000-0000-0000-0000-000000000000")
            result = self.run_profile(env, "run", "secondary")
            self.assertNotEqual(result.returncode, 0,
                                "un pid_file de otro arranque no puede abrir la TUI")
            self.assertIn("could not verify the app-server daemon identity", result.stderr)
            self.assertIn("CODEX_PROFILE_ALLOW_STALE_DAEMON", result.stderr)
            self.assertFalse(marker.exists(), "la TUI se abrió con un pid_file de otro arranque")
            self.assertIsNone(daemon.poll(),
                              "un pid_file de otro arranque no debe autorizar una señal")

    def test_use_invalidates_the_daemon_after_activating(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            result = self.run_profile(env, "use", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(daemon.poll(), "'use' activó el perfil sin invalidar el daemon")

    def test_pi_use_invalidates_the_daemon_after_activating(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            result = self.run_profile(env, "pi", "use", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(daemon.poll(), "'pi use' cambió el auth sin invalidar el daemon")

    def test_read_only_status_does_not_invalidate_the_daemon(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            result = self.run_profile(env, "status")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(daemon.poll(), "un comando de solo lectura tumbó el daemon")

    def test_exec_one_shot_invalidates_the_daemon_without_aborting(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            result = self.run_profile(env, "run", "secondary", "--", "exec", "hola")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(daemon.poll(),
                                 "un one-shot tras un cambio de auth dejó el daemon obsoleto vivo")

    def test_pi_run_invalidates_the_daemon(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            result = self.run_profile(env, "pi", "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(daemon.poll(), "'pi run' cambió el auth sin invalidar el daemon")

    def test_a_daemon_that_ignores_term_is_killed_with_kill(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600, ignore_term=True)
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(daemon.poll(), "con TERM ignorado debía escalar a SIGKILL")

    def test_tui_open_aborts_when_the_stale_daemon_survives(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            pid = self.frozen_survivor_daemon(codex)
            if pid is None:
                self.skipTest("el entorno no permite congelar un cgroup: caso superviviente no ejercitado")
            result = self.run_profile(env, "run", "secondary")
            self.assertNotEqual(result.returncode, 0,
                                "se abrió la TUI con un daemon obsoleto garantizado")
            self.assertIn("CODEX_PROFILE_ALLOW_STALE_DAEMON", result.stderr)
            self.assertTrue(Path(f"/proc/{pid}").exists(),
                            "el daemon superviviente debía seguir vivo (sentido de la prueba)")

    def test_the_escape_hatch_runs_with_a_surviving_daemon(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            env["CODEX_PROFILE_ALLOW_STALE_DAEMON"] = "1"
            pid = self.frozen_survivor_daemon(codex)
            if pid is None:
                self.skipTest("el entorno no permite congelar un cgroup: caso superviviente no ejercitado")
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
    def test_run_invalidates_a_daemon_with_an_unreadable_birth_date(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            # Identidad VÁLIDA pero fecha ilegible: la frescura es desconocida y la
            # política es invalidar, nunca dejar un daemon potencialmente obsoleto.
            payload = json.loads((codex / "app-server-daemon" / "daemon.pid").read_text())
            payload["processStartTime"] = "not a date"
            self.write_daemon_pid(codex, payload)
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(daemon.poll(),
                                 "una fecha ilegible dejó vivo un daemon potencialmente obsoleto")

    def test_tui_open_refuses_when_the_boot_id_cannot_be_read(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "tui-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            # El JSON trae bootId pero la lectura del boot_id del kernel falla: sin
            # contrastar, la identidad no es verificable y NO se señaliza; además
            # la TUI no se abre (una costura de prueba no puede relajar la guarda).
            env["CODEX_PROFILE_BOOT_ID_FILE"] = str(home / "no-such-boot-id")
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            result = self.run_profile(env, "run", "secondary")
            self.assertNotEqual(result.returncode, 0,
                                "una lectura de boot fallida abrió la TUI en silencio")
            self.assertIn("could not verify the app-server daemon identity", result.stderr)
            self.assertIn("CODEX_PROFILE_ALLOW_STALE_DAEMON", result.stderr)
            self.assertFalse(marker.exists(), "la TUI se abrió sin poder contrastar el boot")
            self.assertIsNone(daemon.poll(),
                              "con bootId presente y lectura fallida no debe haber señal")

    def test_tui_open_aborts_when_the_daemon_stop_cannot_be_confirmed(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            # Estado de /proc ilegible (p. ej. EACCES): no se puede declarar «se
            # detuvo», así que NO hay éxito silencioso y la TUI no se abre.
            env["CODEX_PROFILE_TEST_STAT_UNREADABLE_PID"] = str(daemon.pid)
            result = self.run_profile(env, "run", "secondary")
            self.assertNotEqual(result.returncode, 0,
                                "se abrió la TUI sin confirmar que el daemon se detuvo")
            self.assertIn("its stop could not be confirmed", result.stderr)
            self.assertIsNotNone(daemon.poll(),
                                 "la rama no confirmada debía señalizar igualmente (TERM/KILL)")

    def test_the_escape_hatch_runs_when_the_daemon_stop_cannot_be_confirmed(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            env["CODEX_PROFILE_ALLOW_STALE_DAEMON"] = "1"
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600)
            env["CODEX_PROFILE_TEST_STAT_UNREADABLE_PID"] = str(daemon.pid)
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)

    def test_run_login_invalidates_the_daemon_after_the_auth_is_rewritten(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            # El auth vivo YA es secondary y es ANTERIOR al daemon: la invalidación
            # previa lo considera fresco y lo conserva.
            live_codex.write_bytes((accounts / "secondary.auth.json").read_bytes())
            (store / ".current-profile").write_text("secondary\n")
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, born = self.fake_daemon(codex, born_offset_s=-3600)
            self.set_auth_mtime(live_codex, born - 1.0)
            # 'run -- login' (también el wrapper 'codex login') reescribe el auth
            # COMPARTIDO después de la comprobación: hay que invalidar al terminar.
            login = home / "bin" / "codex-login"
            login.write_text(f'#!/bin/sh\ntouch "{live_codex}"\nexit 0\n')
            login.chmod(0o700)
            env["CODEX_BIN"] = str(login)
            result = self.run_profile(env, "run", "secondary", "--", "login")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(daemon.poll(),
                                 "login escribió el auth y el daemon quedó con la cuenta previa")

    def test_run_exec_does_not_invalidate_a_fresh_daemon(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            live_codex.write_bytes((accounts / "secondary.auth.json").read_bytes())
            (store / ".current-profile").write_text("secondary\n")
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, born = self.fake_daemon(codex, born_offset_s=-3600)
            self.set_auth_mtime(live_codex, born - 1.0)
            result = self.run_profile(env, "run", "secondary", "--", "exec", "hola")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNone(daemon.poll(),
                              "un one-shot sin cambio de auth no debe reiniciar el daemon")
    def test_run_login_runs_when_the_daemon_stop_cannot_be_confirmed(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            live_codex.write_bytes((accounts / "secondary.auth.json").read_bytes())
            (store / ".current-profile").write_text("secondary\n")
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, born = self.fake_daemon(codex, born_offset_s=-3600)
            # Daemon OBSOLETO (auth posterior) y estado no confirmable: la guarda
            # de TUI daría rc 2, pero login no sirve cuota por el daemon y sigue.
            self.set_auth_mtime(live_codex, born + 1.0)
            env["CODEX_PROFILE_TEST_STAT_UNREADABLE_PID"] = str(daemon.pid)
            # login no sirve cuota por el daemon: no se bloquea (es vía de
            # reparación); el post-hook avisa de que no se pudo confirmar.
            result = self.run_profile(env, "run", "secondary", "--", "login")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("its stop could not be confirmed", result.stderr)
    def test_the_escape_hatch_opens_the_tui_with_an_unverifiable_daemon(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "tui-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            env["CODEX_PROFILE_ALLOW_STALE_DAEMON"] = "1"
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600, start_ticks=False)
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists(), "el escape explícito debe abrir la TUI")
            self.assertIsNone(daemon.poll(),
                              "ni con el escape se señaliza a un proceso no verificado")

    def test_run_ignores_a_pid_file_pointing_at_a_stranger_process(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "tui-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            # Pid reciclado por un proceso que NO es el daemon: no hay nada nuestro
            # que invalidar y jamás se señaliza a un desconocido.
            stranger = subprocess.Popen(["sleep", "300"])
            self.addCleanup(self.stop_process, stranger)
            born = datetime.datetime.fromtimestamp(time.time() - 3600).replace(microsecond=0)
            self.write_daemon_pid(codex, {
                "pid": stranger.pid,
                "processStartTime": born.strftime("%a %b %d %H:%M:%S %Y"),
                "processIdentity": {"bootId": self.real_boot_id(),
                                    "startTicks": self.process_ticks(stranger.pid)},
            })
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertNotIn("could not verify", result.stderr)
            self.assertTrue(marker.exists(),
                            "un pid_file obsoleto apuntando a un ajeno no debe bloquear la TUI")
            self.assertIsNone(stranger.poll(), "jamás se señaliza a un proceso ajeno")

    def test_pi_run_refuses_when_the_daemon_identity_cannot_be_verified(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "pi-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600, start_ticks=False)
            result = self.run_profile(env, "pi", "run", "secondary")
            self.assertNotEqual(result.returncode, 0,
                                "'pi run' abrió con una identidad no verificable")
            self.assertIn("CODEX_PROFILE_ALLOW_STALE_DAEMON", result.stderr)
            self.assertFalse(marker.exists(), "PI_BIN se ejecutó pese a la guarda")
            self.assertIsNone(daemon.poll(), "no se señaliza a un proceso no verificado")

    def test_the_escape_hatch_lets_pi_run_with_an_unverifiable_daemon(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "pi-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            env["CODEX_PROFILE_ALLOW_STALE_DAEMON"] = "1"
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600, start_ticks=False)
            result = self.run_profile(env, "pi", "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists(), "el escape explícito debe dejar correr PI_BIN")
            self.assertIsNone(daemon.poll(), "no se señaliza a un proceso no verificado")

    def test_use_still_activates_with_an_unverifiable_daemon(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            env = self.quiet_codex_env(home, codex, store, live_pi)
            # 'use' no abre TUI: la identidad no verificable avisa pero no bloquea
            # la activación (solo los caminos de apertura abortan).
            daemon, _ = self.fake_daemon(codex, born_offset_s=-3600, start_ticks=False)
            result = self.run_profile(env, "use", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("could not verify the app-server daemon identity", result.stderr)
            self.assertIsNone(daemon.poll(), "la activación no señaliza sin identidad verificada")

    def test_run_logout_invalidates_the_daemon_after_the_auth_is_rewritten(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            # El auth vivo YA es secondary y es ANTERIOR al daemon: la invalidación
            # previa lo considera fresco y lo conserva.
            live_codex.write_bytes((accounts / "secondary.auth.json").read_bytes())
            (store / ".current-profile").write_text("secondary\n")
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, born = self.fake_daemon(codex, born_offset_s=-3600)
            self.set_auth_mtime(live_codex, born - 1.0)
            # 'run -- logout' (también el wrapper 'codex logout') reescribe/borra el
            # auth COMPARTIDO después de la comprobación: hay que invalidar al terminar.
            logout = home / "bin" / "codex-logout"
            logout.write_text(f'#!/bin/sh\ntouch "{live_codex}"\nexit 0\n')
            logout.chmod(0o700)
            env["CODEX_BIN"] = str(logout)
            result = self.run_profile(env, "run", "secondary", "--", "logout")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIsNotNone(daemon.poll(),
                                 "logout reescribió el auth y el daemon quedó con la cuenta previa")

    def test_run_logout_runs_when_the_daemon_stop_cannot_be_confirmed(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            live_codex.write_bytes((accounts / "secondary.auth.json").read_bytes())
            (store / ".current-profile").write_text("secondary\n")
            env = self.quiet_codex_env(home, codex, store, live_pi)
            daemon, born = self.fake_daemon(codex, born_offset_s=-3600)
            self.set_auth_mtime(live_codex, born + 1.0)
            env["CODEX_PROFILE_TEST_STAT_UNREADABLE_PID"] = str(daemon.pid)
            # logout no sirve cuota por el daemon: no se bloquea; el post-hook avisa.
            result = self.run_profile(env, "run", "secondary", "--", "logout")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertIn("could not be confirmed", result.stderr)

    def decoupled_auth_file(self, home, accounts):
        """AUTH_FILE apuntando a OTRO fichero, ya con el perfil elegido dentro.

        Así el switch no copia, su mtime se queda viejo y el daemon puede nacer
        después: la comprobación de frescura diría FRESH mientras Codex lee el auth
        compartido, que sigue con la cuenta anterior.
        """
        decoupled = home / "decoupled-auth.json"
        decoupled.write_bytes((accounts / "secondary.auth.json").read_bytes())
        return decoupled

    def test_run_refuses_a_decoupled_auth_file_before_opening_the_tui(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "tui-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            decoupled = self.decoupled_auth_file(home, accounts)
            env["AUTH_FILE"] = str(decoupled)
            _daemon, born = self.fake_daemon(codex, born_offset_s=-3600)
            self.set_auth_mtime(decoupled, born - 1.0)
            result = self.run_profile(env, "run", "secondary")
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists(),
                             "la TUI se abrió con AUTH_FILE desacoplado del auth que lee Codex")
            self.assertEqual((store / ".current-profile").read_text().strip(), "shared",
                             "el rechazo ocurrió después del switch: no debe dejar efectos")
            self.assertIn(str(decoupled), result.stderr)
            self.assertIn(str(live_codex), result.stderr)

    def test_the_escape_hatch_opens_when_the_auth_file_is_decoupled(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "tui-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            decoupled = self.decoupled_auth_file(home, accounts)
            env["AUTH_FILE"] = str(decoupled)
            env["CODEX_PROFILE_ALLOW_STALE_DAEMON"] = "1"
            _daemon, born = self.fake_daemon(codex, born_offset_s=-3600)
            self.set_auth_mtime(decoupled, born - 1.0)
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists(), "el escape explícito debe permitir continuar")
            self.assertIn("AUTH_FILE", result.stderr)

    def assert_run_accepts_auth_file_variant(self, variant):
        with self.subTest(variant=variant), tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "tui-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            if variant == "default":
                env.pop("AUTH_FILE")
            elif variant == "symlink":
                (home / "auth-link.json").symlink_to(live_codex)
                env["AUTH_FILE"] = str(home / "auth-link.json")
            else:  # ruta equivalente con '..': realpath coincide con el auth efectivo
                (codex / "unused").mkdir(exist_ok=True)
                env["AUTH_FILE"] = str(codex / "unused" / ".." / "auth.json")
            result = self.run_profile(env, "run", "secondary")
            self.assertEqual(result.returncode, 0, f"{variant}: {result.stderr}")
            self.assertTrue(marker.exists(), f"{variant}: no se abrió la TUI")

    def test_run_accepts_an_auth_file_that_resolves_to_the_same_file(self):
        """La comprobación es de identidad de fichero (realpath), no textual."""
        for variant in ("default", "symlink", "dotdot"):
            self.assert_run_accepts_auth_file_variant(variant)

    def test_pi_run_refuses_a_decoupled_auth_file_before_starting_pi(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "pi-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            decoupled = self.decoupled_auth_file(home, accounts)
            env["AUTH_FILE"] = str(decoupled)
            result = self.run_profile(env, "pi", "run", "secondary")
            self.assertNotEqual(result.returncode, 0, result.stderr)
            self.assertFalse(marker.exists(), "Pi arrancó con AUTH_FILE desacoplado")
            self.assertEqual((store / ".current-profile").read_text().strip(), "shared",
                             "el rechazo ocurrió después del switch: no debe dejar efectos")
            self.assertIn(str(decoupled), result.stderr)

    def test_the_escape_hatch_lets_pi_run_with_a_decoupled_auth_file(self):
        with tempfile.TemporaryDirectory() as root:
            home = Path(root)
            codex, store, accounts, live_codex, live_pi = self.build_store(home)
            marker = home / "pi-ran"
            env = self.marker_env(home, codex, store, live_pi, marker)
            decoupled = self.decoupled_auth_file(home, accounts)
            env["AUTH_FILE"] = str(decoupled)
            env["CODEX_PROFILE_ALLOW_STALE_DAEMON"] = "1"
            result = self.run_profile(env, "pi", "run", "secondary")
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(marker.exists(), "el escape explícito debe permitir arrancar Pi")


if __name__ == "__main__":
    unittest.main()
