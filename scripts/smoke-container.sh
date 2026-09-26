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

token=$(python3 -c "import secrets; print('pst_' + secrets.token_urlsafe(32))")
hash="sha256:$(printf %s "$token" | sha256sum | cut -d' ' -f1)"
cat > "$DATA/config.yaml" <<YAML
clients:
  smoke: {token_hash: "$hash", permissions: [submit_jobs, read_events], recipients: [kate]}
recipients:
  kate: {channels: {fake: {address: kate}}}
scheduler: {quiet_hours: null, min_interval_minutes: 0, jitter_minutes: 0, debounce_seconds: 0}
YAML
chmod -R a+rwX "$DATA"

"$ENGINE" run -d --name "$NAME" -p "$PORT:8000" -v "$DATA:/data:z" \
  -e PESTER_CONFIG=/data/config.yaml -e PESTER_DEV_MODE=true "$IMAGE" >/dev/null

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
"$ENGINE" logs "$NAME" 2>&1 | grep -q "enter code" || { echo "FAIL: no setup code in logs"; exit 1; }
check "setup code logged"
[ "$("$ENGINE" exec "$NAME" id -u)" != 0 ] || { echo "FAIL: runs as root"; exit 1; }; check "non-root"

curl -fs -H "Authorization: Bearer $token" -H 'content-type: application/json' \
  -d '{"recipient_id":"kate","prompt":"Smoke?","response_options":["Yes","No"],"evaluation":{"evaluator":"echo","prompt":"x"}}' \
  "$BASE/api/v1/jobs" >/dev/null
wait_for "prompt delivered" sh -c "curl -fs $BASE/dev/chat/kate | grep -q Smoke"; check "prompt delivered"
curl -fs -H 'content-type: application/json' -d '{"selected_option":"Yes","reply_to":1}' "$BASE/dev/chat/kate" >/dev/null
wait_for "job completed" sh -c \
  "curl -fs -H 'Authorization: Bearer $token' $BASE/api/v1/events | grep -q INTERACTION_COMPLETED"
check "answered, evaluated, completed"
echo "container smoke test passed"
