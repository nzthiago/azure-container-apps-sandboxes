#!/usr/bin/env bash
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
while [[ "$dir" != "/" && ! -f "$dir/.env" ]]; do dir="$(dirname "$dir")"; done
[[ -f "$dir/.env" ]] && { set -a; . "$dir/.env"; set +a; }

LABEL="egress-cli-$$"

echo "==> Creating sandbox (label=$LABEL)..."
aca sandbox create --labels "name=$LABEL" >/dev/null
ID=$(aca sandbox list -l "name=$LABEL" -o json | python -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
echo "    sandbox: $ID"

cleanup() {
  echo "==> Deleting sandbox $ID..."
  aca sandbox delete --id "$ID" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Baseline: curl example.com (Allow by default)..."
aca sandbox exec --id "$ID" -c "curl -sS -o /dev/null -w 'HTTP %{http_code}\n' --max-time 8 https://example.com" || true

echo "==> Set default Deny + allow *.github.com ..."
aca sandbox egress set --id "$ID" --default Deny --host-allow "*.github.com" >/dev/null

echo "==> example.com should now be blocked..."
aca sandbox exec --id "$ID" -c "curl -sS -o /dev/null -w 'HTTP %{http_code}\n' --max-time 8 https://example.com" || echo "    (curl failed = blocked, expected)"

echo "==> api.github.com should still work..."
aca sandbox exec --id "$ID" -c "curl -sS -o /dev/null -w 'HTTP %{http_code}\n' --max-time 8 https://api.github.com" || true

echo "==> Current policy:"
aca sandbox egress show --id "$ID" || true
