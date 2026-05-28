#!/usr/bin/env bash
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
while [[ "$dir" != "/" && ! -f "$dir/.env" ]]; do dir="$(dirname "$dir")"; done
[[ -f "$dir/.env" ]] && { set -a; . "$dir/.env"; set +a; }

LABEL="lc-cli-$$"

aca sandbox create --labels "name=$LABEL" >/dev/null
ID=$(aca sandbox list -l "name=$LABEL" -o json | python -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
echo "==> sandbox: $ID"

cleanup() {
  aca sandbox delete --id "$ID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

state() { aca sandbox get --id "$ID" -o json | python -c "import sys,json;print(json.load(sys.stdin)['state'])"; }

echo "==> state: $(state)"
echo "==> lifecycle set --auto-suspend 60 ..."
aca sandbox lifecycle set --id "$ID" --auto-suspend 60 >/dev/null

echo "==> stop ..."
aca sandbox stop --id "$ID" >/dev/null
sleep 3
echo "    state: $(state)"

echo "==> resume ..."
aca sandbox resume --id "$ID" >/dev/null
sleep 5
echo "    state: $(state)"

echo "==> exec uptime ..."
aca sandbox exec --id "$ID" -c "uptime"
