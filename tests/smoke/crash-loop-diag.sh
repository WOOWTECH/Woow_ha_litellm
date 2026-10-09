#!/usr/bin/env bash
# shellcheck disable=SC2016  # single-quoted programs run inside the container on purpose
# TEMPORARY diagnostics for the crash-loop halt (removed once the fix is verified).
#   tests/smoke/crash-loop-diag.sh <image> <scenario>...
# scenarios:
#   healthy     the smoke test as it is: wait for docker "healthy", break the config, kill litellm
#   live        same, but trigger as soon as /health/liveliness answers (before s6 sees readiness)
#   early       break the config before litellm's first start (crash loop before readiness)
#   pg          bad parameter in postgresql.auto.conf (postgres crash loop before readiness)
set -uo pipefail

IMAGE="${1:?image}"; shift
HERE="$(cd "$(dirname "$0")" && pwd)"
REPO="$(cd "$HERE/../.." && pwd)"
WORK="${RUNNER_TEMP:-/tmp}/crash-diag"
DATA="$WORK/data"
LIMIT="${DIAG_LIMIT:-200}"
mkdir -p "$WORK" "$DATA"
RES="$WORK/results.txt"
: > "$RES"

group() { if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::group::$*"; else echo "== $*"; fi; }
endgroup() { if [ -n "${GITHUB_ACTIONS:-}" ]; then echo "::endgroup::"; fi; }
in_data() { docker run --rm -v "$DATA:/data" --entrypoint sh "$IMAGE" -c "$1"; }
start() { docker run -d --name "$1" -e TZ=Asia/Taipei --stop-timeout 120 -v "$DATA:/data" "$IMAGE" >/dev/null; }
running() { [ "$(docker inspect -f '{{.State.Running}}' "$1" 2>/dev/null)" = true ]; }

snap() { # container label
  docker exec "$1" sh -c '
    echo "--- snapshot '"$2"' $(date +%T.%N | cut -c1-12)"
    for s in litellm postgres; do printf "%-9s %s\n" "$s" "$(/command/s6-svstat /run/service/$s 2>&1)"; done
    for f in /run/woow/failures/*; do [ -f "$f" ] && echo "failures ${f##*/}: $(wc -l < "$f")"; done
    echo "exitcode file: $(cat /run/s6-linux-init-container-results/exitcode 2>/dev/null || echo none)"
    if /command/s6-rc -a list >/dev/null 2>&1; then echo "s6-rc lock: free"; else echo "s6-rc lock: HELD"; fi
    ls /run/service/s6-linux-init-shutdownd/ 2>/dev/null | tr "\n" " "; echo
    ps -o pid,ppid,etime,args | grep -E "s6-rc|rc\.init|rc\.shutdown|s6-svc|s6-svlisten|shutdownd|svc-finish|s6-linux-init-hpr|halt|notifyoncheck" | grep -v grep
  ' 2>&1 || echo "--- snapshot $2: container gone"
}

wait_exit() { # container → sets CODE, DUR (seconds from start of waiting); snapshots every 20 s
  local name="$1" t0 last
  t0="$(date +%s)"; last="$t0"
  while running "$name"; do
    if [ $(( $(date +%s) - t0 )) -ge "$LIMIT" ]; then break; fi
    if [ $(( $(date +%s) - last )) -ge 20 ]; then snap "$name" "t+$(( $(date +%s) - t0 ))s"; last="$(date +%s)"; fi
    sleep 1
  done
  DUR=$(( $(date +%s) - t0 ))
  if running "$name"; then CODE=timeout; snap "$name" "at timeout"; else CODE="$(docker inspect -f '{{.State.ExitCode}}' "$name")"; fi
}

report() { # scenario name
  local sc="$1" name="$2" log="$WORK/$2.log" n fatal tf te gap verdict=PASS
  docker logs -t "$name" > "$log" 2>&1 || true
  n="$(grep -oE '\(([0-9]+) failure\(s\)' "$log" | tr -dc '0-9\n' | sort -n | tail -n1)"
  fatal="$(grep -c 'FATAL failed 5 times' "$log" || true)"
  tf="$(grep -m1 'FATAL failed 5 times' "$log" | cut -d' ' -f1)"
  te="$(docker inspect -f '{{.State.FinishedAt}}' "$name" 2>/dev/null)"
  gap=-
  if [ -n "$tf" ] && [ "$CODE" != timeout ]; then gap=$(( $(date -d "$te" +%s) - $(date -d "$tf" +%s) )); fi
  if [ "$CODE" = timeout ] || [ "$CODE" = 0 ] || [ "${n:-0}" != 5 ] || [ "$fatal" != 1 ] || [ "$gap" = - ] || [ "$gap" -gt 60 ]; then verdict=FAIL; fi
  echo "[$verdict] $sc: exit=$CODE waited=${DUR}s max_failures=${n:-0} fatal_lines=$fatal fatal_to_exit=${gap}s" | tee -a "$RES"
  echo "timeline (s6-rc / finish / halt lines):"
  grep -nE 's6-rc|rc\.init|woow-litellm\] (litellm|postgres):|DIAG|s6-linux-init|s6-svscan|halt|FATAL:  ' "$log" | grep -vE 'ChatGPT' | cut -c1-240 | tail -n 90
  docker rm -f "$name" >/dev/null 2>&1 || true
}

# fresh /data, first boot (migrations), clean stop
cp "$REPO/tests/fixtures/options.default.json" "$DATA/options.json"
group "prepare /data"
start prep
for _ in $(seq 1 450); do
  [ "$(docker inspect -f '{{.State.Health.Status}}' prep)" = healthy ] && break
  running prep || break
  sleep 2
done
docker inspect -f 'prep: {{.State.Health.Status}}' prep
docker stop -t 120 prep >/dev/null; docker rm prep >/dev/null
endgroup

i=0
for sc in "$@"; do
  i=$((i + 1)); name="crash-$sc-$i"
  group "scenario $sc ($name)"
  if [ "$sc" = pg ]; then in_data "echo 'woow_diag_bogus = 1' >> /data/postgres/postgresql.auto.conf"; fi
  start "$name"
  case "$sc" in
    healthy|live)
      t0="$(date +%s)"
      while running "$name" && [ $(( $(date +%s) - t0 )) -lt 600 ]; do
        if [ "$sc" = healthy ]; then
          [ "$(docker inspect -f '{{.State.Health.Status}}' "$name")" = healthy ] && break
          sleep 2
        else
          docker exec "$name" curl -fs -o /dev/null --max-time 1 http://127.0.0.1:4000/health/liveliness 2>/dev/null && break
          sleep 0.2
        fi
      done
      snap "$name" "trigger"
      docker exec "$name" sh -c 'printf "model_list: [\n" > /run/litellm/config.yaml && /command/s6-svc -k /run/service/litellm'
      ;;
    early)
      for _ in $(seq 1 600); do docker exec "$name" sh -c "test -s /run/litellm/config.yaml" 2>/dev/null && break; sleep 0.1; done
      docker exec "$name" sh -c 'printf "model_list: [\n" > /run/litellm/config.yaml'
      snap "$name" "trigger"
      ;;
    pg)
      : # bad parameter written before the start (below)
      ;;
  esac
  wait_exit "$name"
  report "$sc" "$name"
  if [ "$sc" = pg ]; then in_data "sed -i '/^woow_diag_bogus/d' /data/postgres/postgresql.auto.conf"; fi
  endgroup
done

echo "================ results"
cat "$RES"
! grep -q '^\[FAIL\]' "$RES"
