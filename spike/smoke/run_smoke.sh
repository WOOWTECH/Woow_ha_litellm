#!/usr/bin/env bash
# SP1 runtime smoke test of the flattened base image (DESIGN §5.1 checks 4–5, §15 SP1).
#
# usage: run_smoke.sh IMAGE OUTDIR [CONTROL_IMAGE]
# env:   PG_CONTAINER  id of the postgres:18 service container (for psql)
#        PG_URL_FILE   file holding the postgresql:// URL the containers use
#
# Runs on the runner host; LiteLLM containers use --network host so they reach the
# service container (127.0.0.1:5432) and the mock upstream (127.0.0.1:18080).
# Secrets (master key, salt key, UI password) are random per run, masked in the
# Actions log, never echoed, and scrubbed from every file written to OUTDIR.
# CONTROL_IMAGE (optional) is the unmodified upstream image: only the Responses
# background request is repeated against it, to show what pruning changed.
set -uo pipefail

IMAGE=$1
OUT=$2
CONTROL=${3:-}
HERE=$(cd "$(dirname "$0")" && pwd)
mkdir -p "$OUT"
SECRETS=$(mktemp -d)
chmod 700 "$SECRETS"
RESULTS="$OUT/smoke-results.jsonl"
: > "$RESULTS"
FAILED=0

rnd() { openssl rand -hex "$1"; }
MASTER="sk-$(rnd 32)"
SALT="sk-$(rnd 32)"
UIUSER="spike$(rnd 4)"
UIPASS="$(rnd 24)"
if [ -n "${GITHUB_ACTIONS:-}" ]; then
  for s in "$MASTER" "$SALT" "$UIPASS"; do echo "::add-mask::$s"; done
fi
printf '%s' "$UIPASS" > "$SECRETS/uipass"
printf 'Authorization: Bearer %s\n' "$MASTER" > "$SECRETS/auth-header"
DB_URL=$(cat "$PG_URL_FILE")

# record NAME RESULT(PASS|FAIL|INFO) DETAIL [JSON-EXTRA]
record() {
  local extra=${4:-null}
  jq -cn --arg n "$1" --arg r "$2" --arg d "$3" --argjson x "$extra" '{name:$n,result:$r,detail:$d,data:$x}' >> "$RESULTS"
  printf '%-40s %-5s %s\n' "$1" "$2" "$3"
  [ "$2" = FAIL ] && FAILED=1
  return 0
}
scrub() {  # scrub FILE: replace every secret value with ***
  local f=$1
  [ -f "$f" ] || return 0
  python3 -I - "$f" "$MASTER" "$SALT" "$UIPASS" <<'PY'
import sys
p, *secrets = sys.argv[1:]
data = open(p, "rb").read()
for s in secrets:
    data = data.replace(s.encode(), b"***")
open(p, "wb").write(data)
PY
}
trunc() { head -c "${2:-600}" "$1" | tr '\n' ' '; }

common_env() {
  cat <<EOF
DATABASE_URL=$DB_URL
LITELLM_MASTER_KEY=$MASTER
LITELLM_SALT_KEY=$SALT
UI_USERNAME=$UIUSER
UI_PASSWORD=$UIPASS
LITELLM_LOCAL_MODEL_COST_MAP=True
LITELLM_MODE=PRODUCTION
LITELLM_LOG=INFO
EOF
}
common_env > "$SECRETS/env-migrate"; echo "DISABLE_SCHEMA_UPDATE=false" >> "$SECRETS/env-migrate"
common_env > "$SECRETS/env-run"; echo "DISABLE_SCHEMA_UPDATE=true" >> "$SECRETS/env-run"

# --- 4. import ----------------------------------------------------------------
docker run --rm --network none -e LITELLM_LOCAL_MODEL_COST_MAP=True --entrypoint python "$IMAGE" \
  -c "import litellm.proxy.proxy_server; print('import-ok')" > "$OUT/import.log" 2>&1
rc=$?
if [ $rc -eq 0 ] && grep -q import-ok "$OUT/import.log" && ! grep -qE 'ImportError|ModuleNotFoundError|Traceback' "$OUT/import.log"; then
  record import-proxy-server PASS "python -c 'import litellm.proxy.proxy_server' rc=0 (network none)"
else
  record import-proxy-server FAIL "rc=$rc: $(tail -c 600 "$OUT/import.log" | tr '\n' ' ')"
fi

# What the module-level `except Exception` in litellm_logging.py leaves behind (facts C2):
# the same try block also imports the MIT GenericAPILogger, which therefore degrades too.
docker run --rm --network none -e LITELLM_LOCAL_MODEL_COST_MAP=True --entrypoint python "$IMAGE" -c "
from litellm.litellm_core_utils import litellm_logging as l
g = l.GenericAPILogger
print(f'GenericAPILogger={g.__module__}.{g.__name__}; EnterpriseCallbackControls={l.EnterpriseCallbackControls}; EnterpriseStandardLoggingPayloadSetupVAR={l.EnterpriseStandardLoggingPayloadSetupVAR}')
" > "$OUT/degraded-symbols.log" 2>&1
record degraded-symbols INFO "$(tail -c 400 "$OUT/degraded-symbols.log" | tr '\n' ' ')"

# --- 5. migrations on an empty database ---------------------------------------
ndirs=$(docker run --rm --network none --entrypoint sh "$IMAGE" -c \
  'ls -1d /app/.venv/lib/python3.13/site-packages/litellm_proxy_extras/migrations/*/ | wc -l')
t0=$(date +%s)
docker run --rm --network host --env-file "$SECRETS/env-migrate" --entrypoint python "$IMAGE" \
  /app/litellm/proxy/prisma_migration.py > "$OUT/migrate.log" 2>&1
rc=$?
t1=$(date +%s)
scrub "$OUT/migrate.log"
applied=$(docker exec "$PG_CONTAINER" psql -U postgres -d litellm -tAc \
  'select count(*) from "_prisma_migrations" where finished_at is not null and rolled_back_at is null' 2>/dev/null | tr -d '[:space:]')
rows=$(docker exec "$PG_CONTAINER" psql -U postgres -d litellm -tAc 'select count(*) from "_prisma_migrations"' 2>/dev/null | tr -d '[:space:]')
tables=$(docker exec "$PG_CONTAINER" psql -U postgres -d litellm -tAc \
  "select count(*) from information_schema.tables where table_schema='public'" 2>/dev/null | tr -d '[:space:]')
mig_import=$(grep -cE 'ImportError|ModuleNotFoundError' "$OUT/migrate.log" || true)
detail="rc=$rc, migration dirs in image=$ndirs, applied=$applied (rows $rows), public tables=$tables, $((t1 - t0)) s, ImportError lines=$mig_import"
data=$(jq -cn --argjson d "${ndirs:-0}" --argjson a "${applied:-0}" --argjson r "${rows:-0}" --argjson t "${tables:-0}" --argjson s $((t1 - t0)) \
  '{migration_dirs:$d, applied:$a, rows:$r, public_tables:$t, seconds:$s}')
if [ $rc -eq 0 ] && [ -n "$applied" ] && [ "$applied" = "$ndirs" ] && [ "$mig_import" = 0 ]; then
  record migrations PASS "$detail" "$data"
else
  record migrations FAIL "$detail" "$data"
fi

# --- start the mock upstream and LiteLLM --------------------------------------
python3 -I "$HERE/mock_upstream.py" 18080 > "$OUT/mock.log" 2>&1 &
MOCK_PID=$!
start_litellm() {  # start_litellm NAME IMAGE PORT
  docker run -d --name "$1" --network host --env-file "$SECRETS/env-run" \
    -v "$HERE/config.yaml:/cfg/config.yaml:ro" "$2" --port "$3" --config /cfg/config.yaml >/dev/null
}
wait_live() {  # wait_live PORT SECONDS -> prints seconds waited, rc 0 when live
  local i
  for i in $(seq 1 "$2"); do
    if curl -fsS -o /dev/null "http://127.0.0.1:$1/health/liveliness" 2>/dev/null; then echo "$i"; return 0; fi
    sleep 1
  done
  echo "$2"; return 1
}
t0=$(date +%s)
start_litellm spike-litellm "$IMAGE" 4000
waited=$(wait_live 4000 300); live=$?
if [ $live -eq 0 ]; then
  record startup PASS "liveliness after ${waited}s" "{\"seconds_to_live\":$waited}"
else
  record startup FAIL "not live after ${waited}s"
fi

B=http://127.0.0.1:4000
req() {  # req NAME METHOD PATH EXPECTED [curl args...] -> body in $OUT/http-NAME.txt
  local name=$1 method=$2 path=$3 want=$4; shift 4
  local code
  code=$(curl -sS -o "$OUT/http-$name.txt" -w '%{http_code}' -X "$method" "$B$path" "$@" 2>"$OUT/http-$name.err")
  code=$((10#${code:-0}))
  scrub "$OUT/http-$name.txt"
  if [ "$want" = any ]; then
    record "$name" INFO "HTTP $code: $(trunc "$OUT/http-$name.txt" 400)" "{\"status\":$code}"
  elif [ "$code" = "$want" ]; then
    record "$name" PASS "HTTP $code" "{\"status\":$code}"
  else
    record "$name" FAIL "HTTP $code (want $want): $(trunc "$OUT/http-$name.txt" 400)" "{\"status\":$code}"
  fi
}

if [ $live -eq 0 ]; then
  req health-liveliness GET /health/liveliness 200
  req health-readiness GET /health/readiness 200
  if grep -q '"db": *"connected"' "$OUT/http-health-readiness.txt"; then
    record readiness-db PASS "readiness reports db connected"
  else
    record readiness-db FAIL "$(trunc "$OUT/http-health-readiness.txt" 300)"
  fi
  req models-with-key GET /v1/models 200 -H @"$SECRETS/auth-header"
  if grep -q mock-bg "$OUT/http-models-with-key.txt"; then record models-list-has-mock PASS "mock-bg listed"; else record models-list-has-mock FAIL "$(trunc "$OUT/http-models-with-key.txt" 300)"; fi
  req models-without-key GET /v1/models 401

  # UI: form login -> 303 + session cookie, then the dashboard HTML
  code=$(curl -sS -o "$OUT/http-login.txt" -D "$SECRETS/login-headers" -c "$SECRETS/cookies" -w '%{http_code}' \
    -X POST "$B/login" --data-urlencode "username=$UIUSER" --data-urlencode "password@$SECRETS/uipass")
  loc=$(grep -i '^location:' "$SECRETS/login-headers" | tr -d '\r' | cut -d' ' -f2-)
  if [ "$code" = 303 ] && grep -q $'\ttoken\t' "$SECRETS/cookies"; then
    record ui-login PASS "POST /login -> 303, Location $loc, token cookie set"
  else
    record ui-login FAIL "POST /login -> $code, Location $loc"
  fi
  code=$(curl -sS -o "$OUT/http-ui.html" -b "$SECRETS/cookies" -w '%{http_code}' -L --max-redirs 3 "$B/ui/")
  if [ "$code" = 200 ] && grep -qi '<html' "$OUT/http-ui.html"; then
    record ui-get PASS "GET /ui/ with cookie -> 200, $(wc -c < "$OUT/http-ui.html") bytes HTML"
  else
    record ui-get FAIL "GET /ui/ -> $code"
  fi
  rm -f "$OUT/http-ui.html"

  J='Content-Type: application/json'
  req chat-mock POST /v1/chat/completions 200 -H @"$SECRETS/auth-header" -H "$J" \
    -d '{"model":"mock-bg","messages":[{"role":"user","content":"ping"}]}'
  req responses-mock-foreground POST /v1/responses 200 -H @"$SECRETS/auth-header" -H "$J" \
    -d '{"model":"mock-bg","input":"ping"}'
  req responses-nomodel-background POST /v1/responses any -H @"$SECRETS/auth-header" -H "$J" \
    -d '{"model":"no-such-model","input":"ping","background":true}'
  req responses-mock-background POST /v1/responses any -H @"$SECRETS/auth-header" -H "$J" \
    -d '{"model":"mock-bg","input":"ping","background":true}'

  # idle footprint and stop time (SP4 extras)
  sleep 30
  mem=$(docker stats --no-stream --format '{{.MemUsage}}' spike-litellm)
  record idle-memory INFO "docker stats after 30 s idle: $mem"
fi

docker logs spike-litellm > "$OUT/litellm.log" 2>&1
t0=$(date +%s); docker stop -t 30 spike-litellm >/dev/null; t1=$(date +%s)
record stop-time INFO "docker stop took $((t1 - t0)) s"
scrub "$OUT/litellm.log"
# Every ImportError/ModuleNotFoundError line is attributed to the nearest preceding
# file reference. The Responses background request (facts C2, category "error") is
# expected to produce one from response_api_endpoints/endpoints.py; any other is a failure.
verdict=$(python3 -I - "$OUT/litellm.log" <<'PY'
import json, re, sys
lines = open(sys.argv[1], encoding="utf-8", errors="replace").read().splitlines()
hits = []
for i, l in enumerate(lines):
    if re.search(r"ImportError|ModuleNotFoundError", l):
        ctx = "\n".join(lines[max(0, i - 40):i + 1])
        hits.append({"line": i + 1, "text": l.strip()[:200],
                     "expected": "response_api_endpoints/endpoints.py" in ctx})
print(json.dumps({"tracebacks": sum("Traceback" in l for l in lines), "import_errors": len(hits),
                  "unexpected": [h for h in hits if not h["expected"]][:20]}))
PY
)
n_unexp=$(echo "$verdict" | jq '.unexpected | length')
detail="Traceback=$(echo "$verdict" | jq .tracebacks) ImportError/ModuleNotFoundError lines=$(echo "$verdict" | jq .import_errors) unexpected=$n_unexp"
if [ "$n_unexp" = 0 ]; then
  record log-import-errors PASS "$detail" "$verdict"
else
  record log-import-errors FAIL "$detail" "$verdict"
fi
echo "--- import-related log lines ---"
grep -nE 'Traceback|ImportError|ModuleNotFoundError|litellm_enterprise' "$OUT/litellm.log" | head -40

# --- control: the unmodified upstream image, same request ---------------------
if [ -n "$CONTROL" ] && [ $live -eq 0 ]; then
  start_litellm spike-upstream "$CONTROL" 4001
  if waited=$(wait_live 4001 300); then
    B=http://127.0.0.1:4001
    req control-upstream-responses-mock-background POST /v1/responses any -H @"$SECRETS/auth-header" -H 'Content-Type: application/json' \
      -d '{"model":"mock-bg","input":"ping","background":true}'
  else
    record control-upstream-start INFO "upstream image not live after ${waited}s"
  fi
  docker rm -f spike-upstream >/dev/null 2>&1
fi

kill "$MOCK_PID" 2>/dev/null
docker rm -f spike-litellm >/dev/null 2>&1
rm -rf "$SECRETS"
for f in "$OUT"/*; do scrub "$f"; done
jq -s '{result: (if any(.[]; .result=="FAIL") then "FAIL" else "PASS" end), checks: .}' "$RESULTS" > "$OUT/smoke-results.json"
echo "smoke: $(jq -r .result "$OUT/smoke-results.json")"
exit $FAILED
