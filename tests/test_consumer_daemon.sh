#!/usr/bin/env bash
# Prueba en aislamiento de invalidate_managed_daemon() tal y como queda en la
# variante bin/codex-profile-consumer (GNU/Linux), más la integración de la
# política de aborto de command_run/pi run (rc 1 = identidad no verificable,
# rc 2 = superviviente confirmado; solo el escape explícito abre) y la
# invalidación posterior de `run -- login` / `run -- logout`.
#
#   bash tests/test_consumer_daemon.sh [ruta/codex-profile-consumer]
#
# No toca perfiles ni credenciales reales: usa un SHARED_CODEX_HOME temporal, un
# proceso falso (sleep renombrado por argv[0] a "fake-app-server") y un
# daemon.pid sintético con la identidad real de ese proceso.  Los casos que
# necesitan un proceso de otra cuenta (superviviente confirmado) usan sudo -n y
# se saltan si no está disponible.
#
# Los casos 19-22 y 28 (ramas de error de la inspección y de la confirmación) usan
# las costuras de prueba declaradas por la propia función.  Ninguna de ellas
# relaja la protección: un resultado rc 1 bloquea la apertura de TUI igual que
# rc 2 (el marcador reduce accidentes, no es la garantía).
#   CODEX_PROFILE_APPSERVER_BOOT_ID_FILE        ruta alternativa del boot id
#   CODEX_PROFILE_APPSERVER_TEST_STAT_ERROR     fuerza el fallo de lectura de /proc
#   CODEX_PROFILE_APPSERVER_TEST_PIDFD_ERROR    fuerza el fallo del sondeo del pidfd
#   CODEX_PROFILE_APPSERVER_TEST_INSPECT_CRASH  la inspección muere sin veredicto
set -uo pipefail
export LC_ALL=C  # synthetic daemon birth dates must use English weekday/month names

here="$(cd -- "$(dirname -- "${BASH_SOURCE[0]}")" && pwd)"
new="${1:-$here/../bin/codex-profile-consumer}"
failures=0
skipped=0

[[ -f "$new" ]] || { echo "ERROR: no existe $new" >&2; exit 1; }

# --- extracción de la función (stubs del resto del script) -------------------
fn_file="$(mktemp)"
sed -n '/^invalidate_managed_daemon() {/,/^}/p' "$new" > "$fn_file"
if ! grep -q '^invalidate_managed_daemon() {' "$fn_file" \
   || ! grep -q 'pidfd_open' "$fn_file" \
   || ! grep -q '^PY$' "$fn_file"; then
  echo "ERROR: extracción incompleta de la función" >&2
  exit 1
fi

stderr_note() { printf '[stderr] %s\n' "$*" >&2; }
debug_note() { [[ "${DEBUG:-0}" == "1" ]] && printf '[debug] %s\n' "$*" >&2 || true; }
# shellcheck disable=SC1090
source "$fn_file"

ticks_of() {
  awk '{ s=$0; sub(/^.*\) /,"",s); split(s,f," "); print f[20] }' "/proc/$1/stat"
}

proc_state() {
  awk '{ s=$0; sub(/^.*\) /,"",s); split(s,f," "); print f[1] }' "/proc/$1/stat" 2>/dev/null
}

boot_id() { cat /proc/sys/kernel/random/boot_id; }

work="$(mktemp -d "${TMPDIR:-/tmp}/daemon-invalidate-test.XXXXXX")"
fakes=()
root_fakes=()
root_dirs=()
keepers=()
cleanup() {
  local p
  for p in ${fakes[@]+"${fakes[@]}"}; do
    kill -KILL "$p" 2>/dev/null
    wait "$p" 2>/dev/null
  done
  for p in ${keepers[@]+"${keepers[@]}"}; do
    kill -KILL "$p" 2>/dev/null
    wait "$p" 2>/dev/null
  done
  if [[ ${#root_fakes[@]} -gt 0 ]]; then
    sudo -n kill -KILL "${root_fakes[@]}" 2>/dev/null || true
  fi
  if [[ ${#root_dirs[@]} -gt 0 ]]; then
    sudo -n rm -rf "${root_dirs[@]}" 2>/dev/null || true
  fi
  rm -rf "$work" "$fn_file"
}
trap cleanup EXIT

setup() { # setup <caso>
  export SHARED_CODEX_HOME="$work/$1"
  export AUTH_FILE="$SHARED_CODEX_HOME/auth.json"
  mkdir -p "$SHARED_CODEX_HOME/app-server-daemon"
}

# fake_daemon: proceso propio cuyo cmdline contiene "app-server".
fake_daemon() {
  bash -c 'exec -a fake-app-server-daemon /bin/sleep 300' &
  fake=$!
  fakes+=("$fake")
  sleep 0.3
  kill -0 "$fake" 2>/dev/null
}

# fake_daemon_stubborn: ignora SIGTERM (prueba la escalada a SIGKILL).
fake_daemon_stubborn() {
  bash -c 'trap "" TERM; exec -a fake-app-server-daemon /bin/sleep 300' &
  fake=$!
  fakes+=("$fake")
  sleep 0.3
  kill -0 "$fake" 2>/dev/null
}

# fake_daemon_unreaped: hijo vivo de un vigilante que ignora SIGCHLD; cuando el
# daemon lo mate quedará en estado Z (zombi) sin recolectar.
fake_daemon_unreaped() {
  local pid_file="$work/unreaped.pid"
  python3 - "$pid_file" <<'RUNNER' &
import subprocess, sys, time
child = subprocess.Popen(["bash", "-c", "exec -a fake-app-server-daemon /bin/sleep 300"])
with open(sys.argv[1], "w", encoding="utf-8") as handle:
    handle.write(str(child.pid))
time.sleep(300)
RUNNER
  keepers+=("$!")
  sleep 0.8
  fake="$(cat "$pid_file" 2>/dev/null)"
  [[ -n "$fake" ]]
}

# fake_daemon_root: proceso de otra cuenta; no podemos señalizarlo (EPERM).
fake_daemon_root() {
  sudo -n true 2>/dev/null || return 1
  fake="$(sudo -n bash -c 'exec -a fake-app-server-daemon /bin/sleep 300 >/dev/null 2>&1 & echo $!')"
  [[ -n "$fake" ]] || return 1
  root_fakes+=("$fake")
  sleep 0.3
  [[ -d "/proc/$fake" ]]
}

# write_pid_file <startTimeAsctime> <startTicks> [bootId]
write_pid_file() {
  python3 - "$SHARED_CODEX_HOME/app-server-daemon/daemon.pid" "$fake" "$1" "$2" "${3:-$(boot_id)}" <<'PY'
import json, sys
path, pid, start_time, start_ticks, boot = sys.argv[1:]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"pid": int(pid),
               "processStartTime": start_time,
               "processIdentity": {"bootId": boot, "startTicks": int(start_ticks)},
               "executableIdentity": {"digest": "test"}}, f)
PY
}

# write_pid_file_without_ticks <startTimeAsctime>
write_pid_file_without_ticks() {
  python3 - "$SHARED_CODEX_HOME/app-server-daemon/daemon.pid" "$fake" "$1" <<'PY'
import json, sys
path, pid, start_time = sys.argv[1:]
with open(path, "w", encoding="utf-8") as f:
    json.dump({"pid": int(pid), "processStartTime": start_time,
               "processIdentity": {"bootId": "test"},
               "executableIdentity": {"digest": "test"}}, f)
PY
}

check() { # check <nombre> <esperado> <observado>
  if [[ "$2" == "$3" ]]; then
    printf 'ok   %-52s %s\n' "$1" "$3"
  else
    printf 'FAIL %-52s esperado=%s observado=%s\n' "$1" "$2" "$3"
    failures=$((failures + 1))
  fi
}

skip() { printf 'skip %-52s %s\n' "$1" "$2"; skipped=$((skipped + 1)); }

alive() { kill -0 "$fake" 2>/dev/null && echo alive || echo dead; }

# --- integración: la CLI real contra un store temporal ------------------------
int_case() { # int_case <caso> ; deja $int, $MARKER_FILE y el shim listos
  int="$work/$1"
  mkdir -p "$int/home" "$int/codex/app-server-daemon" "$int/store/accounts" "$int/legacy"
  printf '{"tokens":{"access_token":"dummy","account_id":"secondary"}}\n' \
    >"$int/store/accounts/secondary.auth.json"
  cp "$int/store/accounts/secondary.auth.json" "$int/codex/auth.json"
  cat >"$int/codex-shim" <<'SH'
#!/usr/bin/env bash
printf 'shim-ran %s\n' "$*" >>"$MARKER_FILE"
SH
  chmod +x "$int/codex-shim"
  export MARKER_FILE="$int/marker"
  export SHARED_CODEX_HOME="$int/codex"
  export AUTH_FILE="$int/codex/auth.json"
}

run_cli() { # run_cli <args...>  (la CLI real, entorno temporal)
  env HOME="$int/home" \
      CODEX_SHARED_HOME="$int/codex" \
      AUTH_FILE="$int/codex/auth.json" \
      CODEX_PROFILE_STORE="$int/store" \
      CODEX_AUTH_PROFILES_HOME="$int/store/accounts" \
      CODEX_PROFILE_DEFAULT_FILE="$int/store/.default-profile" \
      CODEX_CURRENT_PROFILE_FILE="$int/store/.current-profile" \
      CODEX_PROFILE_LIMITS_HOME="$int/store/limits" \
      CODEX_LEGACY_PROFILE_ROOT="$int/legacy" \
      CODEX_PROFILE_CONSUMER=0 \
      CODEX_BIN="$int/codex-shim" \
      MARKER_FILE="$MARKER_FILE" \
      ${EXTRA_ENV:+$EXTRA_ENV} \
      bash "$new" "$@"
}

# pi_fixture: snapshot de Pi y shim de PI_BIN para los casos de `pi run`.
pi_fixture() {
  printf '{"openai-codex":{"type":"oauth","access":"dummy"}}\n' \
    >"$int/store/accounts/secondary.pi-auth.json"
  cat >"$int/pi-shim" <<'SH'
#!/usr/bin/env bash
printf 'pi-shim-ran %s\n' "$*" >>"$MARKER_FILE"
SH
  chmod +x "$int/pi-shim"
}

# --- caso 1: daemon viejo (nació antes del último auth) -> debe matarlo ------
case1() {
  setup case1
  fake_daemon || { echo "FAIL caso1: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  invalidate_managed_daemon
  check "caso1 daemon viejo: rc" "0" "$?"
  sleep 0.3
  check "caso1 daemon viejo: proceso muerto" "dead" "$(alive)"
}

# --- caso 2: daemon fresco (nació DESPUÉS del último auth) -> no matar -------
case2() {
  setup case2
  fake_daemon || { echo "FAIL caso2: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  touch -d '1 hour ago' "$AUTH_FILE"
  write_pid_file "$(date '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  invalidate_managed_daemon
  check "caso2 daemon fresco: rc" "0" "$?"
  check "caso2 daemon fresco: proceso vivo" "alive" "$(alive)"
}

# --- caso 3: startTicks no cuadra (pid reciclado) -> no señalizar, rc 1 ------
case3() {
  setup case3
  fake_daemon || { echo "FAIL caso3: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(( $(ticks_of "$fake") + 12345 ))"
  invalidate_managed_daemon
  check "caso3 identidad distinta: rc" "1" "$?"
  check "caso3 identidad distinta: proceso vivo" "alive" "$(alive)"
}

# --- caso 4: sin daemon.pid -> no-op ----------------------------------------
case4() {
  setup case4
  : >"$AUTH_FILE"
  invalidate_managed_daemon
  check "caso4 sin daemon.pid: rc" "0" "$?"
}

# --- caso 5: pid que ya no existe -> no-op ----------------------------------
case5() {
  setup case5
  : >"$AUTH_FILE"
  fake=999999
  write_pid_file "Tue Sep 29 08:00:00 2026" 1
  invalidate_managed_daemon
  check "caso5 pid inexistente: rc" "0" "$?"
}

# --- caso 6: cmdline sin "app-server" (pid ajeno) -> rc 0, nunca se señala ---
case6() {
  setup case6
  sleep 300 &
  fake=$!
  fakes+=("$fake")
  sleep 0.3
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  invalidate_managed_daemon
  check "caso6 pid ajeno: rc" "0" "$?"
  check "caso6 pid ajeno: proceso vivo" "alive" "$(alive)"
}

# --- caso 7: processStartTime no parseable -> ante la duda, invalida ---------
case7() {
  setup case7
  fake_daemon || { echo "FAIL caso7: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "no-es-una-fecha" "$(ticks_of "$fake")"
  invalidate_managed_daemon
  check "caso7 fecha ilegible: rc" "0" "$?"
  sleep 0.3
  check "caso7 fecha ilegible: proceso muerto" "dead" "$(alive)"
}

# --- caso 8: sin auth.json -> ante la duda, invalida ------------------------
case8() {
  setup case8
  fake_daemon || { echo "FAIL caso8: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  invalidate_managed_daemon
  check "caso8 sin auth.json: rc" "0" "$?"
  sleep 0.3
  check "caso8 sin auth.json: proceso muerto" "dead" "$(alive)"
}

# --- caso 9: empate de frescura (mismo segundo) -> ambiguo, invalida --------
case9() {
  setup case9
  fake_daemon || { echo "FAIL caso9: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  local now_sec
  now_sec="$(date '+%Y-%m-%d %H:%M:%S')"
  : >"$AUTH_FILE"
  touch -d "$now_sec" "$AUTH_FILE"
  write_pid_file "$(date -d "$now_sec" '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  invalidate_managed_daemon
  check "caso9 empate de frescura: rc" "0" "$?"
  sleep 0.3
  check "caso9 empate de frescura: proceso muerto" "dead" "$(alive)"
}

# --- caso 10: sin startTicks -> identidad incompleta, no señaliza, rc 1 -----
case10() {
  setup case10
  fake_daemon || { echo "FAIL caso10: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file_without_ticks "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')"
  invalidate_managed_daemon
  check "caso10 sin startTicks: rc" "1" "$?"
  check "caso10 sin startTicks: proceso vivo" "alive" "$(alive)"
}

# --- caso 11: bootId distinto -> otro arranque, no señaliza, rc 1 -----------
case11() {
  setup case11
  fake_daemon || { echo "FAIL caso11: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")" "00000000-0000-0000-0000-000000000000"
  invalidate_managed_daemon
  check "caso11 bootId distinto: rc" "1" "$?"
  check "caso11 bootId distinto: proceso vivo" "alive" "$(alive)"
}

# --- caso 12: ignora SIGTERM -> escala a SIGKILL y confirma, rc 0 -----------
case12() {
  setup case12
  fake_daemon_stubborn || { echo "FAIL caso12: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  invalidate_managed_daemon
  check "caso12 TERM ignorado: rc" "0" "$?"
  sleep 0.3
  check "caso12 TERM ignorado: proceso muerto" "dead" "$(alive)"
}

# --- caso 13: superviviente confirmado (proceso de otra cuenta) -> rc 2 -----
case13() {
  setup case13
  if ! fake_daemon_root; then
    skip "caso13 superviviente (sudo): rc" "sin sudo -n"
    return
  fi
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  invalidate_managed_daemon
  check "caso13 superviviente (sudo): rc" "2" "$?"
  [[ -d "/proc/$fake" ]] && check "caso13 superviviente (sudo): sigue vivo" "alive" "alive" \
                         || check "caso13 superviviente (sudo): sigue vivo" "alive" "dead"
}

# --- caso 14: ya es un zombi -> terminado al instante, sin señalizar --------
case14() {
  setup case14
  fake_daemon_unreaped || { echo "FAIL caso14: no arrancó el proceso no recolectado"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  kill -TERM "$fake" 2>/dev/null
  local waited=0
  while [[ "$(proc_state "$fake")" != "Z" && "$waited" -lt 30 ]]; do
    sleep 0.1
    waited=$((waited + 1))
  done
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  local t0 t1
  t0="$(date +%s)"
  invalidate_managed_daemon
  local rc=$?
  t1="$(date +%s)"
  check "caso14 zombi: rc" "0" "$rc"
  check "caso14 zombi: estado Z" "Z" "$(proc_state "$fake")"
  [[ $((t1 - t0)) -lt 2 ]] && check "caso14 zombi: sin espera de 2 s" "fast" "fast" \
                           || check "caso14 zombi: sin espera de 2 s" "fast" "slow($((t1 - t0))s)"
}

# --- caso 15: integración -- exec con daemon obsoleto propio: lo mata, rc 0 --
case15() {
  int_case case15
  fake_daemon || { echo "FAIL caso15: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  run_cli run secondary -- exec true
  check "caso15 exec con daemon obsoleto: rc" "0" "$?"
  sleep 0.3
  check "caso15 exec con daemon obsoleto: daemon muerto" "dead" "$(alive)"
  [[ -f "$MARKER_FILE" ]] && check "caso15 exec: la CLI corrió" "ran" "ran" \
                          || check "caso15 exec: la CLI corrió" "ran" "no-ran"
}

# --- caso 16: integración TUI con superviviente -> aborta visible -----------
case16() {
  int_case case16
  if ! fake_daemon_root; then
    skip "caso16 TUI aborta (sudo): rc" "sin sudo -n"
    return
  fi
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  local err
  err="$(run_cli run secondary -- 2>&1 >/dev/null)"
  check "caso16 TUI aborta (sudo): rc" "1" "$?"
  [[ -f "$MARKER_FILE" ]] && check "caso16 TUI aborta: no abrió sesión" "no-ran" "ran" \
                          || check "caso16 TUI aborta: no abrió sesión" "no-ran" "no-ran"
  case "$err" in
    *"still running with the previous account"*) check "caso16 TUI aborta: mensaje visible" "yes" "yes" ;;
    *) check "caso16 TUI aborta: mensaje visible" "yes" "no" ;;
  esac
}

# --- caso 17: misma TUI con la variable de escape -> abre, rc 0 -------------
case17() {
  int_case case17
  if ! fake_daemon_root; then
    skip "caso17 TUI con escape (sudo): rc" "sin sudo -n"
    return
  fi
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  EXTRA_ENV="CODEX_PROFILE_ALLOW_STALE_DAEMON=1" run_cli run secondary --
  check "caso17 TUI con escape (sudo): rc" "0" "$?"
  [[ -f "$MARKER_FILE" ]] && check "caso17 TUI con escape: abrió sesión" "ran" "ran" \
                          || check "caso17 TUI con escape: abrió sesión" "ran" "no-ran"
  EXTRA_ENV=""
}

# --- caso 18: integración -- exec con superviviente -> avisa pero corre -----
case18() {
  int_case case18
  if ! fake_daemon_root; then
    skip "caso18 exec con superviviente (sudo): rc" "sin sudo -n"
    return
  fi
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  run_cli run secondary -- exec true
  check "caso18 exec con superviviente (sudo): rc" "0" "$?"
  [[ -f "$MARKER_FILE" ]] && check "caso18 exec con superviviente: corrió" "ran" "ran" \
                          || check "caso18 exec con superviviente: corrió" "ran" "no-ran"
}

# --- caso 19: bootId presente pero ilegible -> identidad no verificable ------
case19() {
  setup case19
  fake_daemon || { echo "FAIL caso19: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  local err
  err="$(CODEX_PROFILE_APPSERVER_BOOT_ID_FILE=/nonexistent/boot-id invalidate_managed_daemon 2>&1 >/dev/null)"
  check "caso19 bootId ilegible: rc" "1" "$?"
  check "caso19 bootId ilegible: proceso vivo" "alive" "$(alive)"
  case "$err" in
    *"cannot read this boot's id"*) check "caso19 bootId ilegible: aviso" "yes" "yes" ;;
    *) check "caso19 bootId ilegible: aviso" "yes" "no" ;;
  esac
}

# --- caso 20: ni /proc ni el pidfd pueden informar -> rc 2, no "confirmado" --
case20() {
  setup case20
  fake_daemon || { echo "FAIL caso20: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  local err
  err="$(CODEX_PROFILE_APPSERVER_TEST_STAT_ERROR=1 CODEX_PROFILE_APPSERVER_TEST_PIDFD_ERROR=1 \
         invalidate_managed_daemon 2>&1 >/dev/null)"
  check "caso20 confirmación ilegible: rc" "2" "$?"
  sleep 0.3
  check "caso20 confirmación ilegible: proceso muerto" "dead" "$(alive)"
  case "$err" in
    *"could not confirm"*) check "caso20 confirmación ilegible: aviso" "yes" "yes" ;;
    *) check "caso20 confirmación ilegible: aviso" "yes" "no" ;;
  esac
}

# --- caso 28: con /proc ilegible el pidfd SÍ confirma la salida -> rc 0 -----
case28() {
  setup case28
  fake_daemon || { echo "FAIL caso28: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  CODEX_PROFILE_APPSERVER_TEST_STAT_ERROR=1 invalidate_managed_daemon
  check "caso28 /proc ilegible: rc" "0" "$?"
  sleep 0.3
  check "caso28 /proc ilegible: proceso muerto" "dead" "$(alive)"
}

# --- caso 21: daemon.pid corrupto -> aviso y rc 1, nunca "nada que hacer" ----
case21() {
  setup case21
  fake_daemon || { echo "FAIL caso21: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  printf 'esto no es json\n' >"$SHARED_CODEX_HOME/app-server-daemon/daemon.pid"
  local err
  err="$(invalidate_managed_daemon 2>&1 >/dev/null)"
  check "caso21 pid corrupto: rc" "1" "$?"
  check "caso21 pid corrupto: proceso vivo" "alive" "$(alive)"
  case "$err" in
    *"pid file is unreadable"*) check "caso21 pid corrupto: aviso" "yes" "yes" ;;
    *) check "caso21 pid corrupto: aviso" "yes" "no" ;;
  esac
}

# --- caso 22: la inspección muere sin veredicto -> aviso y rc 1 --------------
case22() {
  setup case22
  fake_daemon || { echo "FAIL caso22: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  local err
  err="$(CODEX_PROFILE_APPSERVER_TEST_INSPECT_CRASH=1 invalidate_managed_daemon 2>&1 >/dev/null)"
  check "caso22 inspección sin veredicto: rc" "1" "$?"
  check "caso22 inspección sin veredicto: proceso vivo" "alive" "$(alive)"
  case "$err" in
    *"unexpected result"*) check "caso22 inspección sin veredicto: aviso" "yes" "yes" ;;
    *) check "caso22 inspección sin veredicto: aviso" "yes" "no" ;;
  esac
}

# --- caso 23: modo consumidor, dos aperturas -> el daemon NO se reinicia -----
case23() {
  int_case case23
  printf '{"openai-codex":{"type":"oauth","access":"dummy"}}\n' \
    >"$int/store/accounts/secondary.pi-auth.json"
  EXTRA_ENV="CODEX_PROFILE_CONSUMER=1" run_cli use secondary
  check "caso23 consumidor: perfil por defecto" "0" "$?"
  EXTRA_ENV="CODEX_PROFILE_CONSUMER=1" run_cli run secondary -- exec true
  check "caso23 consumidor: primera apertura" "0" "$?"
  sleep 1.2
  fake_daemon || { echo "FAIL caso23: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  write_pid_file "$(date '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  EXTRA_ENV="CODEX_PROFILE_CONSUMER=1" run_cli run secondary -- exec true
  check "caso23 consumidor: segunda apertura" "0" "$?"
  check "caso23 consumidor: daemon no reiniciado" "alive" "$(alive)"
  EXTRA_ENV=""
}

# --- caso 24: pi run con superviviente -> aborta visible --------------------
case24() {
  int_case case24
  if ! fake_daemon_root; then
    skip "caso24 pi run aborta (sudo): rc" "sin sudo -n"
    return
  fi
  pi_fixture
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  local err
  err="$(EXTRA_ENV="PI_BIN=$int/pi-shim PI_AUTH_FILE=$int/pi-auth.json" run_cli pi run secondary -- --help 2>&1 >/dev/null)"
  check "caso24 pi run aborta (sudo): rc" "1" "$?"
  [[ -f "$MARKER_FILE" ]] && grep -q 'pi-shim-ran' "$MARKER_FILE" \
    && check "caso24 pi run aborta: no arrancó" "no-ran" "ran" \
    || check "caso24 pi run aborta: no arrancó" "no-ran" "no-ran"
  case "$err" in
    *"still running with the previous account"*) check "caso24 pi run aborta: mensaje" "yes" "yes" ;;
    *) check "caso24 pi run aborta: mensaje" "yes" "no" ;;
  esac
}

# --- caso 25: la misma pi run con la variable de escape -> arranca ----------
case25() {
  int_case case25
  if ! fake_daemon_root; then
    skip "caso25 pi run con escape (sudo): rc" "sin sudo -n"
    return
  fi
  pi_fixture
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  EXTRA_ENV="PI_BIN=$int/pi-shim PI_AUTH_FILE=$int/pi-auth.json CODEX_PROFILE_ALLOW_STALE_DAEMON=1" \
    run_cli pi run secondary -- --help
  check "caso25 pi run con escape (sudo): rc" "0" "$?"
  [[ -f "$MARKER_FILE" ]] && grep -q 'pi-shim-ran' "$MARKER_FILE" \
    && check "caso25 pi run con escape: arrancó" "ran" "ran" \
    || check "caso25 pi run con escape: arrancó" "ran" "no-ran"
  EXTRA_ENV=""
}

# --- caso 26: run -- login reescribe el auth -> invalida DESPUÉS ------------
case26() {
  int_case case26
  fake_daemon || { echo "FAIL caso26: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  touch -d '1 hour ago' "$AUTH_FILE"
  write_pid_file "$(date '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  cat >"$int/codex-shim" <<'SH'
#!/usr/bin/env bash
printf 'shim-ran %s\n' "$*" >>"$MARKER_FILE"
printf '{"tokens":{"access_token":"dummy2","account_id":"otra"}}\n' >"$AUTH_FILE"
SH
  chmod +x "$int/codex-shim"
  run_cli run secondary -- login
  check "caso26 login: rc" "0" "$?"
  sleep 0.3
  check "caso26 login: daemon invalidado tras el login" "dead" "$(alive)"
  [[ -f "$MARKER_FILE" ]] && grep -q 'shim-ran' "$MARKER_FILE" \
    && check "caso26 login: la CLI corrió" "ran" "ran" \
    || check "caso26 login: la CLI corrió" "ran" "no-ran"
}

# --- caso 27: login con superviviente -> no bloquea el one-shot -------------
case27() {
  int_case case27
  if ! fake_daemon_root; then
    skip "caso27 login con superviviente (sudo): rc" "sin sudo -n"
    return
  fi
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  run_cli run secondary -- login
  check "caso27 login con superviviente (sudo): rc" "0" "$?"
  [[ -f "$MARKER_FILE" ]] && grep -q 'shim-ran' "$MARKER_FILE" \
    && check "caso27 login con superviviente: corrió" "ran" "ran" \
    || check "caso27 login con superviviente: corrió" "ran" "no-ran"
}

# --- caso 29: identidad no verificable (boot ilegible) -> la TUI NO abre ----
case29() {
  int_case case29
  fake_daemon || { echo "FAIL caso29: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  local err
  err="$(EXTRA_ENV="CODEX_PROFILE_APPSERVER_BOOT_ID_FILE=/nonexistent/boot-id" \
        run_cli run secondary -- 2>&1 >/dev/null)"
  check "caso29 identidad no verificable: rc" "1" "$?"
  [[ -f "$MARKER_FILE" ]] && check "caso29 identidad no verificable: no abrió" "no-ran" "ran" \
                          || check "caso29 identidad no verificable: no abrió" "no-ran" "no-ran"
  case "$err" in
    *"could not be verified"*) check "caso29 identidad no verificable: mensaje" "yes" "yes" ;;
    *) check "caso29 identidad no verificable: mensaje" "yes" "no" ;;
  esac
  check "caso29 identidad no verificable: daemon vivo" "alive" "$(alive)"
  EXTRA_ENV=""
}

# --- caso 30: la misma TUI con el escape -> abre, rc 0 ----------------------
case30() {
  int_case case30
  fake_daemon || { echo "FAIL caso30: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  EXTRA_ENV="CODEX_PROFILE_APPSERVER_BOOT_ID_FILE=/nonexistent/boot-id CODEX_PROFILE_ALLOW_STALE_DAEMON=1" \
    run_cli run secondary --
  check "caso30 escape identidad no verificable: rc" "0" "$?"
  [[ -f "$MARKER_FILE" ]] && check "caso30 escape: abrió sesión" "ran" "ran" \
                          || check "caso30 escape: abrió sesión" "ran" "no-ran"
  EXTRA_ENV=""
}

# --- caso 31: consumidor contenido idéntico -> repara permisos sin tocar mtime -
case31() {
  int_case case31
  # Primera publicación: deja el payload canónico en el auth del consumidor.
  EXTRA_ENV="CODEX_PROFILE_CONSUMER=1" run_cli run secondary -- exec true
  check "caso31 consumidor: primera apertura" "0" "$?"
  chmod 0644 "$AUTH_FILE"
  touch -d '2 hours ago' "$AUTH_FILE"
  chmod 0644 "$AUTH_FILE"
  touch -d '2 hours ago' "$AUTH_FILE"
  local before after
  before="$(stat -c %Y "$AUTH_FILE")"
  fake_daemon || { echo "FAIL caso31: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  write_pid_file "$(date '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  EXTRA_ENV="CODEX_PROFILE_CONSUMER=1" run_cli run secondary -- exec true
  check "caso31 consumidor: apertura con auth dañado" "0" "$?"
  after="$(stat -c %Y "$AUTH_FILE")"
  check "caso31 permisos reparados" "600" "$(stat -c %a "$AUTH_FILE")"
  check "caso31 mtime intacto" "$before" "$after"
  check "caso31 directorio del auth en 700" "700" "$(stat -c %a "$int/codex")"
  check "caso31 daemon no reiniciado" "alive" "$(alive)"
  EXTRA_ENV=""
}

# --- caso 32: run -- logout limpia el auth -> invalida DESPUÉS ---------------
case32() {
  int_case case32
  fake_daemon || { echo "FAIL caso32: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  touch -d '1 hour ago' "$AUTH_FILE"
  write_pid_file "$(date '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  cat >"$int/codex-shim" <<'SH'
#!/usr/bin/env bash
printf 'shim-ran %s\n' "$*" >>"$MARKER_FILE"
rm -f "$AUTH_FILE"
SH
  chmod +x "$int/codex-shim"
  run_cli run secondary -- logout
  check "caso32 logout: rc" "0" "$?"
  sleep 0.3
  check "caso32 logout: daemon invalidado tras el logout" "dead" "$(alive)"
  [[ -f "$MARKER_FILE" ]] && grep -q 'shim-ran' "$MARKER_FILE" \
    && check "caso32 logout: la CLI corrió" "ran" "ran" \
    || check "caso32 logout: la CLI corrió" "ran" "no-ran"
}

# --- caso 33: pid ajeno -> la TUI abre sin bloquear (sin falsos bloqueos) ---
case33() {
  int_case case33
  sleep 300 &
  fake=$!
  fakes+=("$fake")
  sleep 0.3
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  run_cli run secondary --
  check "caso33 pid ajeno: la TUI abre" "0" "$?"
  check "caso33 pid ajeno: proceso vivo" "alive" "$(alive)"
  [[ -f "$MARKER_FILE" ]] && check "caso33 pid ajeno: la CLI corrió" "ran" "ran" \
                          || check "caso33 pid ajeno: la CLI corrió" "ran" "no-ran"
}

# --- caso 34: pi run con identidad no verificable -> aborta visible ---------
case34() {
  int_case case34
  pi_fixture
  fake_daemon || { echo "FAIL caso34: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  : >"$AUTH_FILE"
  write_pid_file "$(date -d '1 hour ago' '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  local err
  err="$(EXTRA_ENV="PI_BIN=$int/pi-shim PI_AUTH_FILE=$int/pi-auth.json CODEX_PROFILE_APPSERVER_BOOT_ID_FILE=/nonexistent/boot-id" \
        run_cli pi run secondary -- --help 2>&1 >/dev/null)"
  check "caso34 pi run identidad no verificable: rc" "1" "$?"
  [[ -f "$MARKER_FILE" ]] && grep -q 'pi-shim-ran' "$MARKER_FILE" \
    && check "caso34 pi run identidad no verificable: no arrancó" "no-ran" "ran" \
    || check "caso34 pi run identidad no verificable: no arrancó" "no-ran" "no-ran"
  case "$err" in
    *"could not be verified"*) check "caso34 pi run identidad no verificable: mensaje" "yes" "yes" ;;
    *) check "caso34 pi run identidad no verificable: mensaje" "yes" "no" ;;
  esac
  EXTRA_ENV=""
}

# --- caso 35: AUTH_FILE desacoplado del CODEX_HOME efectivo -> RECHAZA -------
# El switch y la frescura miran AUTH_FILE, pero Codex lee
# $SHARED_CODEX_HOME/auth.json: con un alternativo que ya tiene el perfil y
# mtime viejo, sin la comprobación la TUI abriría con el auth real anterior.
case35() {
  int_case case35
  cp "$int/store/accounts/secondary.auth.json" "$int/codex/other-auth.json"
  touch -d '2 hours ago' "$int/codex/other-auth.json"
  : >"$AUTH_FILE"
  touch -d '1 hour ago' "$AUTH_FILE"
  fake_daemon || { echo "FAIL caso35: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  write_pid_file "$(date '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  local err
  err="$(EXTRA_ENV="AUTH_FILE=$int/codex/other-auth.json" run_cli run secondary -- 2>&1 >/dev/null)"
  check "caso35 AUTH_FILE desacoplado: rc" "1" "$?"
  if [[ -f "$MARKER_FILE" ]] && grep -q 'shim-ran' "$MARKER_FILE"; then
    check "caso35 AUTH_FILE desacoplado: no abrió sesión" "no-ran" "ran"
  else
    check "caso35 AUTH_FILE desacoplado: no abrió sesión" "no-ran" "no-ran"
  fi
  case "$err" in
    *"other-auth.json"*) check "caso35 AUTH_FILE desacoplado: mensaje con la ruta" "yes" "yes" ;;
    *) check "caso35 AUTH_FILE desacoplado: mensaje con la ruta" "yes" "no" ;;
  esac
  check "caso35 AUTH_FILE desacoplado: daemon no señalizado" "alive" "$(alive)"
  EXTRA_ENV=""
}

# --- caso 36: mismo desacople con el escape explícito -> abre ---------------
case36() {
  int_case case36
  cp "$int/store/accounts/secondary.auth.json" "$int/codex/other-auth.json"
  touch -d '2 hours ago' "$int/codex/other-auth.json"
  : >"$AUTH_FILE"
  touch -d '1 hour ago' "$AUTH_FILE"
  fake_daemon || { echo "FAIL caso36: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  write_pid_file "$(date '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  local err
  err="$(EXTRA_ENV="AUTH_FILE=$int/codex/other-auth.json CODEX_PROFILE_ALLOW_STALE_DAEMON=1" run_cli run secondary -- 2>&1 >/dev/null)"
  check "caso36 escape desacople: rc" "0" "$?"
  if [[ -f "$MARKER_FILE" ]] && grep -q 'shim-ran' "$MARKER_FILE"; then
    check "caso36 escape desacople: abrió sesión" "ran" "ran"
  else
    check "caso36 escape desacople: abrió sesión" "ran" "no-ran"
  fi
  case "$err" in
    *"other-auth.json"*) check "caso36 escape desacople: aviso con la ruta" "yes" "yes" ;;
    *) check "caso36 escape desacople: aviso con la ruta" "yes" "no" ;;
  esac
  EXTRA_ENV=""
}

# --- caso 37: AUTH_FILE equivalente (symlink) -> no se rechaza --------------
case37() {
  int_case case37
  ln -s "$int/codex" "$int/codex-link"
  : >"$AUTH_FILE"
  touch -d '1 hour ago' "$AUTH_FILE"
  fake_daemon || { echo "FAIL caso37: no arrancó el proceso falso"; failures=$((failures + 1)); return; }
  write_pid_file "$(date '+%a %b %e %H:%M:%S %Y')" "$(ticks_of "$fake")"
  EXTRA_ENV="AUTH_FILE=$int/codex-link/auth.json" run_cli run secondary --
  check "caso37 AUTH_FILE equivalente (symlink): rc" "0" "$?"
  if [[ -f "$MARKER_FILE" ]] && grep -q 'shim-ran' "$MARKER_FILE"; then
    check "caso37 AUTH_FILE equivalente: abrió sesión" "ran" "ran"
  else
    check "caso37 AUTH_FILE equivalente: abrió sesión" "ran" "no-ran"
  fi
  EXTRA_ENV=""
}

# --- caso 38: AUTH_FILE vacío -> valor por defecto (coincide) ---------------
case38() {
  int_case case38
  : >"$AUTH_FILE"
  touch -d '1 hour ago' "$AUTH_FILE"
  EXTRA_ENV="AUTH_FILE=" run_cli run secondary -- exec true
  check "caso38 AUTH_FILE vacío: rc" "0" "$?"
  if [[ -f "$MARKER_FILE" ]] && grep -q 'shim-ran' "$MARKER_FILE"; then
    check "caso38 AUTH_FILE vacío: la CLI corrió" "ran" "ran"
  else
    check "caso38 AUTH_FILE vacío: la CLI corrió" "ran" "no-ran"
  fi
  EXTRA_ENV=""
}

# --- caso 39: pi run con AUTH_FILE desacoplado -> rechaza; escape abre ------
case39() {
  int_case case39
  pi_fixture
  cp "$int/store/accounts/secondary.auth.json" "$int/codex/other-auth.json"
  touch -d '2 hours ago' "$int/codex/other-auth.json"
  : >"$AUTH_FILE"
  touch -d '1 hour ago' "$AUTH_FILE"
  local err
  err="$(EXTRA_ENV="AUTH_FILE=$int/codex/other-auth.json PI_BIN=$int/pi-shim PI_AUTH_FILE=$int/pi-auth.json" \
        run_cli pi run secondary -- --help 2>&1 >/dev/null)"
  check "caso39 pi run desacoplado: rc" "1" "$?"
  if [[ -f "$MARKER_FILE" ]] && grep -q 'pi-shim-ran' "$MARKER_FILE"; then
    check "caso39 pi run desacoplado: no arrancó" "no-ran" "ran"
  else
    check "caso39 pi run desacoplado: no arrancó" "no-ran" "no-ran"
  fi
  case "$err" in
    *"other-auth.json"*) check "caso39 pi run desacoplado: mensaje con la ruta" "yes" "yes" ;;
    *) check "caso39 pi run desacoplado: mensaje con la ruta" "yes" "no" ;;
  esac
  EXTRA_ENV="AUTH_FILE=$int/codex/other-auth.json PI_BIN=$int/pi-shim PI_AUTH_FILE=$int/pi-auth.json CODEX_PROFILE_ALLOW_STALE_DAEMON=1" \
    run_cli pi run secondary -- --help
  check "caso39 pi run con escape: rc" "0" "$?"
  if [[ -f "$MARKER_FILE" ]] && grep -q 'pi-shim-ran' "$MARKER_FILE"; then
    check "caso39 pi run con escape: arrancó" "ran" "ran"
  else
    check "caso39 pi run con escape: arrancó" "ran" "no-ran"
  fi
  EXTRA_ENV=""
}

# --- caso 40: modos 0700/0400/01600 del auth -> reparados a 0600 sin tocar mtime --
case40() {
  int_case case40
  EXTRA_ENV="CODEX_PROFILE_CONSUMER=1" run_cli run secondary -- exec true
  check "caso40 consumidor: primera publicación" "0" "$?"
  local mode before after applied
  for mode in 0700 0400 01600; do
    chmod "$mode" "$AUTH_FILE"
    # El modo pedido debe estar realmente puesto: con 01600 el bit especial
    # (setuid) es justo lo que la comprobación estricta debe ver.  Si el sistema
    # de ficheros lo descartase, el caso no probaría nada y lo decimos.
    applied="$(stat -c %a "$AUTH_FILE")"
    if [[ "$applied" != "${mode#0}" ]]; then
      skip "caso40 modo $mode aplicado" "el sistema de ficheros no conserva el bit ($applied)"
      continue
    fi
    check "caso40 modo $mode aplicado" "${mode#0}" "$applied"
    touch -d '2 hours ago' "$AUTH_FILE"
    before="$(stat -c %Y "$AUTH_FILE")"
    EXTRA_ENV="CODEX_PROFILE_CONSUMER=1" run_cli run secondary -- exec true
    check "caso40 modo $mode: rc" "0" "$?"
    check "caso40 modo $mode reparado a 0600" "600" "$(stat -c %a "$AUTH_FILE")"
    check "caso40 modo $mode mtime intacto" "$before" "$(stat -c %Y "$AUTH_FILE")"
  done
  check "caso40 directorio del auth en 700" "700" "$(stat -c %a "$int/codex")"
  EXTRA_ENV=""
}

# --- caso 41: CODEX_HOME no asegurable (0700) -> falla CERRADO --------------
case41() {
  int_case case41
  if ! sudo -n true 2>/dev/null; then
    skip "caso41 CODEX_HOME no asegurable (sudo): rc" "sin sudo -n"
    return
  fi
  sudo -n mkdir -p "$int/codex-root"
  sudo -n chmod 0755 "$int/codex-root"
  sudo -n chown root:root "$int/codex-root"
  root_dirs+=("$int/codex-root")
  local err
  err="$(EXTRA_ENV="CODEX_SHARED_HOME=$int/codex-root AUTH_FILE=$int/codex-root/auth.json" \
        run_cli run secondary -- exec true 2>&1 >/dev/null)"
  check "caso41 CODEX_HOME no asegurable: rc" "1" "$?"
  if [[ -f "$MARKER_FILE" ]] && grep -q 'shim-ran' "$MARKER_FILE"; then
    check "caso41 CODEX_HOME no asegurable: no corrió" "no-ran" "ran"
  else
    check "caso41 CODEX_HOME no asegurable: no corrió" "no-ran" "no-ran"
  fi
  case "$err" in
    *"mode 700"*) check "caso41 CODEX_HOME no asegurable: mensaje del modo" "yes" "yes" ;;
    *) check "caso41 CODEX_HOME no asegurable: mensaje del modo" "yes" "no" ;;
  esac
  EXTRA_ENV=""
}

# --- caso 42: auth de otra cuenta -> falla CERRADO --------------------------
case42() {
  int_case case42
  if ! sudo -n true 2>/dev/null; then
    skip "caso42 auth ajeno (sudo): rc" "sin sudo -n"
    return
  fi
  EXTRA_ENV="CODEX_PROFILE_CONSUMER=1" run_cli run secondary -- exec true
  check "caso42 consumidor: primera publicación" "0" "$?"
  sudo -n chown root:root "$AUTH_FILE"
  sudo -n chmod 0644 "$AUTH_FILE"
  local err
  err="$(EXTRA_ENV="CODEX_PROFILE_CONSUMER=1" run_cli run secondary -- exec true 2>&1 >/dev/null)"
  check "caso42 auth ajeno: rc" "1" "$?"
  case "$err" in
    *"Cannot secure"*) check "caso42 auth ajeno: mensaje de seguridad" "yes" "yes" ;;
    *) check "caso42 auth ajeno: mensaje de seguridad" "yes" "no" ;;
  esac
  EXTRA_ENV=""
}

echo "== invalidate_managed_daemon: $new =="
case1; case2; case3; case4; case5; case6
case7; case8; case9; case10; case11; case12
case13; case14; case15; case16; case17; case18
case19; case20; case21; case22; case23; case24
case25; case26; case27; case28; case29; case30
case31; case32; case33; case34; case35; case36
case37; case38; case39; case40; case41; case42

if [[ "$failures" -eq 0 ]]; then
  echo "TODO OK ($((42 - skipped)) casos, $skipped saltados)"
  exit 0
fi
echo "FALLOS: $failures"
exit 1
