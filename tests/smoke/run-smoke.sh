#!/usr/bin/env bash
# shellcheck disable=SC2016  # single-quoted awk/sh programs on purpose
# Container smoke test of the add-on image on the CI runner (DESIGN v3.2 §12.2), M1 must-haves:
#   (a) boot A, no SUPERVISOR_TOKEN (local mode): PostgreSQL up, every migration applied, LiteLLM
#       ready, nginx 8099 serves the help page, http://127.0.0.1:4000/ redirects;
#   (b) boot B with a Supervisor emulator (ingress_entry) and an HA Ingress emulator: sign-in, /ui and
#       _next through the prefix, plugin page and API, bridge URL with the prefix, framing headers;
#   (c) boot B is a restart on the same /data: data still there, plugin selfcheck ok;
#   (d) headless Chromium through the Ingress emulator: no JS error, the bridge loads.
# Nothing is pushed or uploaded; secrets are read from the container and masked in the log.
#
#   tests/smoke/run-smoke.sh <image>        (needs docker; PW_PYTHON = a python with playwright)
set -euo pipefail

IMAGE="${1:?usage: $0 <image>}"
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="${RUNNER_TEMP:-/tmp}/woow-smoke"
DATA="$WORK/data"
NET=woow-hassio
ADDON_IP=172.30.33.10
SUP_IP=172.30.33.2
MOCK_IP=172.30.33.20
INGRESS_IP=172.30.32.2
PW_PYTHON="${PW_PYTHON:-python3}"
SUMMARY="${GITHUB_STEP_SUMMARY:-$WORK/summary.md}"

mkdir -p "$WORK" "$DATA"
export ADDON="$ADDON_IP" INGRESS="http://$INGRESS_IP:8080" RESULTS="$WORK/results.jsonl" STATE_FILE="$WORK/state.json"
export MOCK_UPSTREAM="http://$MOCK_IP:18080"
: > "$RESULTS"

mask() { if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::add-mask::$1"; fi; }
group() { if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::group::$*"; else echo "== $*"; fi; }
endgroup() { if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::endgroup::"; fi; }
result() { # name status detail
  python3 -c 'import json,sys; print(json.dumps({"check": sys.argv[1], "status": sys.argv[2], "detail": sys.argv[3][:300]}))' \
    "$1" "$2" "${3:-}" >> "$RESULTS"
  echo "[$2] $1${3:+ — $3}"
}
req() { # name, then a command that must succeed
  local name="$1"; shift
  if "$@"; then result "$name" PASS; else result "$name" FAIL; fi
}
dump_logs() {
  for c in "$@"; do
    group "docker logs $c"
    docker logs "$c" 2>&1 | tail -n 400 || true
    endgroup
  done
}
in_addon() { docker exec "$CUR" "$@"; }
psql_addon() { in_addon /command/s6-setuidgid postgres /usr/bin/psql -X -q -tA -h /run/postgresql -U postgres -d litellm -c "$1"; }

start_addon() { # name, extra docker args…
  local name="$1"; shift
  docker run -d --name "$name" --hostname 1b7b4ce7-woow-litellm --network "$NET" --ip "$ADDON_IP" \
    -e TZ=Asia/Taipei --stop-timeout 120 -v "$DATA:/data" "$@" "$IMAGE" >/dev/null
  CUR="$name"
}

wait_healthy() { # name, limit seconds → prints seconds
  local name="$1" limit="$2" t0 st running
  t0="$(date +%s)"
  while :; do
    st="$(docker inspect -f '{{.State.Health.Status}}' "$name" 2>/dev/null || echo gone)"
    running="$(docker inspect -f '{{.State.Running}}' "$name" 2>/dev/null || echo false)"
    if [ "$st" = healthy ]; then echo $(( $(date +%s) - t0 )); return 0; fi
    if [ "$running" != true ]; then echo "container $name stopped (exit $(docker inspect -f '{{.State.ExitCode}}' "$name"))" >&2; return 1; fi
    if [ $(( $(date +%s) - t0 )) -ge "$limit" ]; then echo "container $name not healthy after ${limit}s" >&2; return 1; fi
    sleep 2
  done
}

# /proc/<pid>/environ of another uid needs CAP_SYS_PTRACE, which `docker exec` (root) does not
# have: read it as the process's own uid instead. Empty output means "could not read" → fail.
proc_uid() { in_addon awk '/^Uid:/ {print $2}' "/proc/$1/status"; }
proc_env_names() { # pattern → variable names (never values) of the oldest matching process
  local pid uid
  pid="$(in_addon pgrep -o -f "$1" || true)"
  [ -n "$pid" ] || return 1
  uid="$(proc_uid "$pid")"
  docker exec -u "$uid" "$CUR" sh -c "tr '\\0' '\\n' < /proc/$pid/environ | cut -d= -f1"
}
proc_env_value() { # pattern, variable → value (only for non-secret variables)
  local pid uid
  pid="$(in_addon pgrep -o -f "$1")"
  uid="$(proc_uid "$pid")"
  docker exec -u "$uid" "$CUR" sh -c "tr '\\0' '\\n' < /proc/$pid/environ | sed -n 's/^$2=//p'"
}

docker network create --subnet 172.30.32.0/23 --gateway 172.30.32.1 "$NET" >/dev/null
cp "$REPO/tests/fixtures/options.default.json" "$DATA/options.json"
# OpenAI-compatible stand-in (Responses API background mode), on the same network as the add-on.
docker run -d --name mock-upstream --network "$NET" --ip "$MOCK_IP" -v "$HERE:/smoke:ro" \
  --entrypoint /app/.venv/bin/python "$IMAGE" -I /smoke/mock_upstream.py 18080 0.0.0.0 >/dev/null

IMAGE_BYTES="$(docker image inspect -f '{{.Size}}' "$IMAGE")"
LAYERS="$(docker image inspect -f '{{len .RootFS.Layers}}' "$IMAGE")"

# ── (a) boot A: local mode ────────────────────────────────────────────────────
group "boot A (no Supervisor token)"
start_addon woow-a
if ! FIRST_BOOT="$(wait_healthy woow-a 900)"; then dump_logs woow-a; exit 1; fi
echo "healthy after ${FIRST_BOOT}s"
MASTER="$(in_addon cat /data/secrets/master_key)"; mask "$MASTER"
UIPW="$(in_addon cat /data/secrets/ui_password)"; mask "$UIPW"
PGPW="$(in_addon cat /data/secrets/pg_password)"; mask "$PGPW"
SALT="$(in_addon cat /data/secrets/salt_key)"; mask "$SALT"
export WOOW_MASTER_KEY="$MASTER" WOOW_UI_PASSWORD="$UIPW" WOOW_UI_USER=admin
endgroup

group "checks (a)"
result "a/first boot to healthy" PASS "${FIRST_BOOT}s"
MIG_DIRS="$(in_addon /app/.venv/bin/python -I -c 'import os, litellm_proxy_extras as m; d = os.path.join(os.path.dirname(m.__file__), "migrations"); print(sum(os.path.isdir(os.path.join(d, x)) for x in os.listdir(d)))')"
MIG_DONE="$(psql_addon "SELECT count(*) FROM _prisma_migrations WHERE finished_at IS NOT NULL AND rolled_back_at IS NULL")"
if [ "$MIG_DIRS" -gt 100 ] && [ "$MIG_DIRS" = "$MIG_DONE" ]; then result "a/all migrations applied" PASS "$MIG_DONE/$MIG_DIRS"; else result "a/all migrations applied" FAIL "$MIG_DONE/$MIG_DIRS"; fi
PGV="$(in_addon /usr/bin/postgres --version)"
req "a/PostgreSQL 18 ($PGV)" grep -q ' 18\.' <<< "$PGV"
req "a/PostgreSQL listens on 127.0.0.1:5432 only" sh -c "docker exec $CUR netstat -tln | grep ':5432 ' | grep -qv '127.0.0.1:5432' && exit 1 || docker exec $CUR netstat -tln | grep -q '127.0.0.1:5432 '"
req "a/LiteLLM listening on 4000" sh -c "docker exec $CUR netstat -tln | grep -q ':4000 '"
INDEXES="$(psql_addon "SELECT count(*) FROM pg_index i JOIN pg_class c ON c.oid = i.indexrelid WHERE c.relname IN ('LiteLLM_SpendLogs_api_key_startTime_idx','LiteLLM_SpendLogs_litellm_call_id_idx') AND i.indisvalid")"
if [ "$INDEXES" = 2 ]; then result "a/SpendLogs hand-built indexes valid" PASS; else result "a/SpendLogs hand-built indexes valid" WARN "$INDEXES/2 (oneshot may still run)"; fi
HELP="$(docker run --rm --network "$NET" --ip "$INGRESS_IP" --entrypoint /usr/bin/curl "$IMAGE" -fsS "http://$ADDON_IP:8099/")"
req "a/nginx 8099 serves the help page (no ingress_entry)" grep -q '這次開機無法在側邊欄顯示' <<< "$HELP"
req "a/help page has no secret" sh -c "! printf '%s' \"\$1\" | grep -qF -e \"\$2\" -e \"\$3\" -e \"\$4\" -e \"\$5\"" _ "$HELP" "$MASTER" "$UIPW" "$PGPW" "$SALT"
STATUS="$(docker run --rm --network "$NET" --ip "$INGRESS_IP" --entrypoint /usr/bin/curl "$IMAGE" -fsS "http://$ADDON_IP:8099/status")"
req "a/help /status → readiness (db connected)" grep -q '"db":"connected"' <<< "$STATUS"
CODE="$(curl -s -o /dev/null -w '%{http_code}' "http://$ADDON_IP:8099/")"
req "a/8099 from another address → 403 ($CODE)" test "$CODE" = 403
LOC="$(in_addon curl -s -o /dev/null -w '%{http_code} %{redirect_url}' http://127.0.0.1:4000/)"
req "a/http://127.0.0.1:4000/ redirects to /ui/ ($LOC)" grep -qE '^30[1278] http://127\.0\.0\.1:4000/ui/$' <<< "$LOC"
python3 "$HERE/smoke_checks.py" local || true
endgroup

group "processes and environment (boot A)"
in_addon ps -o user,pid,args | grep -E 'postgres -D|bin/litellm|nginx' | grep -v grep || true
req "a/LiteLLM runs as nobody (65534)" sh -c "docker exec $CUR ps -o user,args | grep -E '[b]in/litellm' | grep -q '^nobody'"
req "a/PostgreSQL runs as postgres" sh -c "docker exec $CUR ps -o user,args | grep -E '[p]ostgres -D' | grep -q '^postgres'"
WORKER="$(in_addon pgrep -n -f 'nginx: worker' || true)"
req "a/nginx workers run as uid 101 (woow-nginx), not the LiteLLM uid" test "$(proc_uid "${WORKER:-0}" 2>/dev/null)" = 101
LENV="$(proc_env_names '/app/.venv/bin/litellm' || true)"
req "a/litellm env readable for the checks" test -n "$LENV"
req "a/litellm env has PYTHONPATH (plugin on), SSL_CERT_FILE, TZ" sh -c "printf '%s\n' \"\$1\" | grep -qx PYTHONPATH && printf '%s\n' \"\$1\" | grep -qx SSL_CERT_FILE && printf '%s\n' \"\$1\" | grep -qx TZ" _ "$LENV"
req "a/litellm FORWARDED_ALLOW_IPS=127.0.0.1" test "$(proc_env_value '/app/.venv/bin/litellm' FORWARDED_ALLOW_IPS)" = 127.0.0.1
req "a/litellm has no SERVER_ROOT_PATH in local mode" sh -c "! printf '%s\n' \"\$1\" | grep -qx SERVER_ROOT_PATH" _ "$LENV"
req "a/CHATGPT_TOKEN_DIR on tmpfs" sh -c "d=\$(docker exec $CUR readlink -f /run/woow-chatgpt); docker exec $CUR awk -v d=\"\$d\" '\$3==\"tmpfs\" && (d==\$2 || index(d, \$2\"/\")==1)' /proc/mounts | grep -q ."
endgroup

group "stop boot A"
T1="$(date +%s)"
docker stop -t 120 woow-a >/dev/null
STOP_A=$(( $(date +%s) - T1 ))
docker logs woow-a > "$WORK/boot-a.log" 2>&1
CTRL="$(docker run --rm -v "$DATA:/data" --entrypoint /usr/bin/pg_controldata "$IMAGE" /data/postgres | grep 'Database cluster state' || true)"
req "a/clean PostgreSQL shutdown ($CTRL; stop ${STOP_A}s)" grep -q 'shut down' <<< "$CTRL"
req "a/log has no secret" sh -c "! grep -qF -e \"\$1\" -e \"\$2\" -e \"\$3\" -e \"\$4\" \"\$5\"" _ "$MASTER" "$UIPW" "$PGPW" "$SALT" "$WORK/boot-a.log"
req "a/log has no access-log lines" sh -c "! grep -qE '\"(GET|POST) /[^ ]* HTTP/1\\.[01]\" [0-9]{3}' \"\$1\"" _ "$WORK/boot-a.log"
req "a/log shows the Woow plugin page" grep -q 'ChatGPT subscription page at /woow/chatgpt' "$WORK/boot-a.log"
docker rm woow-a >/dev/null
endgroup

# ── (b)(c) boot B: Supervisor + HA Ingress emulators, same /data ──────────────
group "emulators"
SUP_TOKEN="$(python3 -c 'import secrets; print(secrets.token_hex(24))')"; mask "$SUP_TOKEN"
ENTRY="/api/hassio_ingress/$(python3 -c 'import secrets; print(secrets.token_urlsafe(32))')"
export ENTRY
docker run -d --name supervisor-emu --network "$NET" --ip "$SUP_IP" --network-alias supervisor \
  -e SUP_TOKEN="$SUP_TOKEN" -e INGRESS_ENTRY="$ENTRY" -e OPTIONS_JSON="$(cat "$DATA/options.json")" \
  -e SUP_VERSION=2026.10.1 -e CORE_VERSION=2026.10.0 \
  -v "$HERE:/smoke:ro" --entrypoint /app/.venv/bin/python "$IMAGE" -I /smoke/supervisor_emulator.py 80 >/dev/null
docker run -d --name ingress-emu --network "$NET" --ip "$INGRESS_IP" \
  -v "$HERE:/smoke:ro" --entrypoint /app/.venv/bin/python "$IMAGE" -I /smoke/ha_ingress_emulator.py 8080 "http://$ADDON_IP:8099" "${ENTRY##*/}" >/dev/null
for _ in $(seq 1 30); do curl -fs "http://$SUP_IP/__test/state" >/dev/null && break; sleep 1; done
endgroup

group "boot B (Supervisor token, ingress_entry)"
start_addon woow-b -e SUPERVISOR_TOKEN="$SUP_TOKEN" -e HASSIO_TOKEN="$SUP_TOKEN"
if ! SECOND_BOOT="$(wait_healthy woow-b 900)"; then dump_logs woow-b supervisor-emu ingress-emu; exit 1; fi
echo "healthy after ${SECOND_BOOT}s"
endgroup

group "checks (b) (c)"
result "c/restart to healthy" PASS "${SECOND_BOOT}s"
SEL="$(in_addon /command/emptyenv /command/s6-envdir /run/woow/env/litellm /command/s6-setuidgid nobody /app/.venv/bin/python /usr/share/woow-litellm/py/woow_chatgpt.py selfcheck || true)"
req "c/plugin selfcheck ($SEL)" python3 -c 'import json,sys; d=json.loads(sys.argv[1]); sys.exit(0 if d == {"ok": True, "drift": []} else 1)' "$SEL"
req "c/secrets unchanged after the restart" test "$(in_addon cat /data/secrets/master_key)" = "$MASTER"
SUPSTATE="$(curl -fs "http://$SUP_IP/__test/state")"
req "b/Supervisor emulator saw /info and /addons/self/info" python3 -c 'import json,sys; s=json.loads(sys.argv[1]); p={c["path"] for c in s["calls"]}; sys.exit(0 if {"/info","/addons/self/info"} <= p else 1)' "$SUPSTATE"
req "b/master key and UI password written back once, other options kept" python3 -c '
import json, os, sys
s = json.loads(sys.argv[1]); posts = s["posts"]
ok = len(posts) == 1 and posts[0]["options"].get("master_key") == os.environ["WOOW_MASTER_KEY"] \
     and posts[0]["options"].get("ui_password") == os.environ["WOOW_UI_PASSWORD"] \
     and posts[0]["options"].get("spend_logs_retention") == "30d"
sys.exit(0 if ok else 1)' "$SUPSTATE"
LENV="$(proc_env_names '/app/.venv/bin/litellm')"
req "b/litellm has SERVER_ROOT_PATH = ingress_entry" test "$(proc_env_value '/app/.venv/bin/litellm' SERVER_ROOT_PATH)" = "$ENTRY"
for pat in '/app/.venv/bin/litellm' 'postgres -D' 'nginx: master'; do
  NAMES="$(proc_env_names "$pat" || true)"
  req "b/no SUPERVISOR_TOKEN in the environment of '$pat' ($(printf '%s\n' "$NAMES" | grep -c .) variables read)" \
    sh -c '[ -n "$1" ] && ! printf "%s\n" "$1" | grep -qE "^(SUPERVISOR_TOKEN|HASSIO_TOKEN)$"' _ "$NAMES"
done
req "b/token removed from the s6 container environment" sh -c "! docker exec $CUR ls /run/s6/container_environment | grep -qE '^(SUPERVISOR_TOKEN|HASSIO_TOKEN)$'"
req "b/UI files rewritten: no /litellm-asset-prefix left" sh -c "! docker exec $CUR grep -rIl /litellm-asset-prefix /var/lib/litellm/ui | grep -q ."
python3 "$HERE/smoke_checks.py" ingress || true
endgroup

group "browser (d)"
"$PW_PYTHON" "$HERE/browser_check.py" || true
endgroup

group "idle memory"
sleep 90
MEM="$(docker stats --no-stream --format '{{.MemUsage}}' woow-b)"
echo "idle memory: $MEM"
endgroup

docker logs woow-b > "$WORK/boot-b.log" 2>&1 || true
req "b/log has no secret" sh -c "! grep -qF -e \"\$1\" -e \"\$2\" -e \"\$3\" -e \"\$4\" \"\$5\"" _ "$MASTER" "$UIPW" "$PGPW" "$SALT" "$WORK/boot-b.log"
group "logs (boot A, boot B, emulators)"
cat "$WORK/boot-a.log"
echo "----------------------------------------------------------------- boot B"
cat "$WORK/boot-b.log"
echo "----------------------------------------------------------------- emulators"
docker logs supervisor-emu 2>&1 | tail -n 50 || true
docker logs ingress-emu 2>&1 | tail -n 50 || true
docker logs mock-upstream 2>&1 | tail -n 20 || true
endgroup

T1="$(date +%s)"
docker stop -t 120 woow-b >/dev/null
STOP_B=$(( $(date +%s) - T1 ))
docker rm woow-b >/dev/null

# ── extras from §12.2: restore (7), salt guard (9), option validation (10) ────
expect_refusal() { # name, log pattern: the container must stop by itself, non-zero, with the reason
  local name="$1" pattern="$2" code
  code="$(timeout 120 docker wait "$name" || echo timeout)"
  docker logs "$name" > "$WORK/$name.log" 2>&1 || true
  if [ "$code" != timeout ] && [ "$code" != 0 ] && grep -q -- "$pattern" "$WORK/$name.log"; then
    result "x/$name: refused to start (exit $code)" PASS
  else
    result "x/$name: refused to start" FAIL "exit $code; reason '$pattern' $(grep -c -- "$pattern" "$WORK/$name.log") time(s) in the log"
    tail -n 40 "$WORK/$name.log"
  fi
  docker rm -f "$name" >/dev/null 2>&1 || true
}
in_data() { docker run --rm -v "$DATA:/data" --entrypoint sh "$IMAGE" -c "$1"; }
put_options() { docker run --rm -i -v "$DATA:/data" --entrypoint sh "$IMAGE" -c 'cat > /data/options.json' < "$1"; }

group "extra: HA restore leaves root-owned 0644 files (§12.2 item 7)"
in_data 'chown -R 0:0 /data && find /data -type d -exec chmod 755 {} + && find /data -type f -exec chmod 644 {} +'
start_addon woow-restore
if RESTORE_BOOT="$(wait_healthy woow-restore 600)"; then
  result "x/restore: owners and modes fixed, healthy" PASS "${RESTORE_BOOT}s"
  req "x/restore: secrets back to 0600 root" sh -c "[ \"\$(docker exec $CUR stat -c '%a %u' /data/secrets/master_key)\" = '600 0' ]"
  req "x/restore: PGDATA back to postgres 0700" sh -c "[ \"\$(docker exec $CUR stat -c '%a %u' /data/postgres)\" = '700 70' ]"
else
  result "x/restore: owners and modes fixed, healthy" FAIL
  docker logs woow-restore 2>&1 | tail -n 60
fi
docker stop -t 120 woow-restore >/dev/null 2>&1 || true
docker rm woow-restore >/dev/null 2>&1 || true
endgroup

group "extra: salt key guard (§12.2 item 9)"
in_data 'cp -p /data/secrets/salt_key /data/salt.keep && printf "sk-%064d" 7 > /data/secrets/salt_key'
start_addon woow-salt
expect_refusal woow-salt "different salt key"
in_data 'mv /data/salt.keep /data/secrets/salt_key'
in_data 'cp -p /data/secrets/salt_key /data/salt.keep && rm -f /data/secrets/salt_key'
start_addon woow-nosalt
expect_refusal woow-nosalt "salt_key is missing"
in_data 'mv /data/salt.keep /data/secrets/salt_key'
endgroup

group "extra: option validation (§12.2 item 10)"
in_data 'cat /data/options.json' > "$WORK/options.keep"
python3 -c 'import json,sys; o=json.load(open(sys.argv[1])); o["master_key"]="sk-1234"; json.dump(o, open(sys.argv[2], "w"))' "$WORK/options.keep" "$WORK/options.try"
put_options "$WORK/options.try"
start_addon woow-weak
expect_refusal woow-weak "master_key"
for key in FORWARDED_ALLOW_IPS LITELLM_DANGEROUSLY_PERMIT_WEAK_OR_UNSET_MASTER_KEY CHATGPT_AUTH_FILE; do
  python3 -c 'import json,sys; o=json.load(open(sys.argv[1])); o["env_vars"]=[{"key": sys.argv[2], "value": "x"}]; json.dump(o, open(sys.argv[3], "w"))' "$WORK/options.keep" "$key" "$WORK/options.try"
  put_options "$WORK/options.try"
  start_addon "woow-env-$(echo "$key" | tr 'A-Z_' 'a-z-' | cut -c1-20)"
  expect_refusal "$CUR" "cannot be overridden"
done
put_options "$WORK/options.keep"
endgroup

group "extra: LiteLLM keeps crashing → the container stops (§12.2 item 19)"
start_addon woow-crash
if wait_healthy woow-crash 600 >/dev/null; then
  in_addon sh -c 'printf "model_list: [\n" > /run/litellm/config.yaml && /command/s6-svc -k /run/service/litellm'
  code="$(timeout 300 docker wait woow-crash || echo timeout)"
  docker logs woow-crash > "$WORK/woow-crash.log" 2>&1 || true
  if [ "$code" != timeout ] && [ "$code" != 0 ] && grep -q 'failed 5 times within 5 minutes' "$WORK/woow-crash.log"; then
    result "x/crash loop: container stopped with exit $code after 5 failures" PASS
  else
    result "x/crash loop: container stopped after 5 failures" FAIL "exit $code"
    tail -n 60 "$WORK/woow-crash.log"
  fi
else
  result "x/crash loop: container stopped after 5 failures" FAIL "did not become healthy first"
fi
docker rm -f woow-crash >/dev/null 2>&1 || true
endgroup

FAILED="$(grep -c '"status": "FAIL"' "$RESULTS" || true)"
WARNED="$(grep -c '"status": "WARN"' "$RESULTS" || true)"
PASSED="$(grep -c '"status": "PASS"' "$RESULTS" || true)"
{
  echo "## Woow LiteLLM smoke (amd64)"
  echo ""
  echo "| item | value |"
  echo "|---|---|"
  echo "| image size (uncompressed, docker) | $(( IMAGE_BYTES / 1048576 )) MiB, $LAYERS layers |"
  echo "| first boot (fresh /data) to healthy | ${FIRST_BOOT}s |"
  echo "| restart to healthy | ${SECOND_BOOT}s |"
  echo "| stop (docker stop -t 120) | boot A ${STOP_A}s, boot B ${STOP_B}s |"
  echo "| idle memory (90 s after checks) | $MEM |"
  echo "| migrations | $MIG_DONE / $MIG_DIRS |"
  echo "| checks | $PASSED pass, $WARNED warn, $FAILED fail |"
  echo ""
  echo "| check | status | detail |"
  echo "|---|---|---|"
  python3 -c '
import json, sys
for line in open(sys.argv[1]):
    r = json.loads(line)
    d = r["detail"].replace("|", "\\|").replace("\n", " ")
    c, st = r["check"].replace("|", "\\|"), r["status"]
    print(f"| {c} | {st} | {d} |")' "$RESULTS"
} >> "$SUMMARY"
cp "$RESULTS" "$WORK/results.final.jsonl"
echo "summary: $PASSED pass, $WARNED warn, $FAILED fail"
[ "$FAILED" = 0 ]
