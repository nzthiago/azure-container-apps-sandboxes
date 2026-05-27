#!/usr/bin/env bash
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
while [[ "$dir" != "/" && ! -f "$dir/.env" ]]; do dir="$(dirname "$dir")"; done
[[ -f "$dir/.env" ]] && { set -a; . "$dir/.env"; set +a; }

TENANT="t-$(date +%s)"

cleanup() {
  IDS=$(aca sandbox list -l "tenant=$TENANT" -o json 2>/dev/null | python -c "import sys,json;print(' '.join(s['id'] for s in json.load(sys.stdin)))")
  for id in $IDS; do
    aca sandbox delete --id "$id" >/dev/null 2>&1 || true
  done
}
trap cleanup EXIT

for i in 0 1 2; do
  ROLE=$([[ $i -lt 2 ]] && echo "worker" || echo "control")
  NAME="sbx-$TENANT-$i"
  echo "==> Create $NAME (role=$ROLE)..."
  aca sandbox create --labels "name=$NAME,tenant=$TENANT,role=$ROLE" >/dev/null
done

echo
echo "==> Workers under tenant=$TENANT:"
aca sandbox list -l "tenant=$TENANT,role=worker"
echo
echo "==> Control under tenant=$TENANT:"
aca sandbox list -l "tenant=$TENANT,role=control"
