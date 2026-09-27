#!/usr/bin/env bash
# Build the image and run one job through it end to end on the mock channel.
# Usage: scripts/smoke-container.sh   (uses docker, or podman if docker is missing; override with ENGINE=)
set -euo pipefail

ENGINE=${ENGINE:-$(command -v docker || command -v podman)}
IMAGE=${IMAGE:-pester:smoke}
NAME=pester-smoke-$$
PORT=${PORT:-18000}
BASE=http://127.0.0.1:$PORT
DATA=$(mktemp -d)
trap '"$ENGINE" rm -f "$NAME" >/dev/null 2>&1 || true; rm -rf "$DATA"' EXIT

cd "$(dirname "$0")/.."
"$ENGINE" build --format docker -t "$IMAGE" . 2>/dev/null || "$ENGINE" build -t "$IMAGE" .

chmod a+rwX "$DATA"
# No YAML: everything is configured through the CLI, the way a Dockge user would. Dev mode is on only for
# the /dev chat routes this script answers through.
"$ENGINE" run -d --name "$NAME" -p "$PORT:8000" -v "$DATA:/data:z" -e PESTER_DEV_MODE=true "$IMAGE" >/dev/null
pester() { "$ENGINE" exec "$NAME" pester "$@"; }

wait_for() {  # wait_for <description> <command...>
  local what=$1; shift
  for _ in $(seq 30); do "$@" >/dev/null 2>&1 && return 0; sleep 1; done
  echo "FAIL: $what"; "$ENGINE" logs "$NAME"; exit 1
}
check() { echo "ok: $1"; }

wait_for "/health" curl -fs "$BASE/health"; check "/health"
wait_for "/ready" curl -fs "$BASE/ready"; check "/ready"
curl -fs "$BASE/admin/setup" | grep -q "Setup code" && curl -fs -o /dev/null "$BASE/admin/static/htmx.min.js" \
  || { echo "FAIL: admin UI templates or static files missing from the image"; exit 1; }; check "admin UI"
# The container's log capture can lag the process slightly, so wait for the line rather than reading once.
wait_for "setup code in logs" sh -c "'$ENGINE' logs '$NAME' 2>&1 | grep -q 'enter code'"
check "setup code logged"
[ "$("$ENGINE" exec "$NAME" id -u)" != 0 ] || { echo "FAIL: runs as root"; exit 1; }; check "non-root"

pester channel add mock --type mock >/dev/null
pester recipient add kate >/dev/null
pester recipient link kate mock address=kate >/dev/null
pester settings set scheduler.quiet_hours=null scheduler.min_interval_minutes=0 scheduler.jitter_minutes=0 \
  scheduler.debounce_seconds=0 >/dev/null
token=$(pester client create smoke --recipient kate | awk 'NF==1 && length($1) > 20 {print $1}')
[ -n "$token" ] || { echo "FAIL: no client token"; exit 1; }
check "configured with the CLI"
job='{"recipient_id":"kate","prompt":"Smoke?","response_options":["Yes","No"],"evaluation":{"evaluator":"rule","prompt":"x"}}'
# The server applies CLI changes within a few seconds; until then the new token is unknown.
wait_for "job accepted" curl -fs -H "Authorization: Bearer $token" -H 'content-type: application/json' -d "$job" \
  "$BASE/api/v1/jobs"
check "job accepted with the new token"
wait_for "prompt delivered" sh -c "curl -fs '$BASE/dev/chat/kate?channel=mock' | grep -q Smoke"; check "prompt delivered"
curl -fs -H 'content-type: application/json' -d '{"selected_option":"Yes","reply_to":1}' \
  "$BASE/dev/chat/kate?channel=mock" >/dev/null
wait_for "job completed" sh -c \
  "curl -fs -H 'Authorization: Bearer $token' $BASE/api/v1/events | grep -q INTERACTION_COMPLETED"
check "answered, evaluated, completed"
echo "container smoke test passed"
