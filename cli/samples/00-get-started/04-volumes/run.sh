#!/usr/bin/env bash
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
while [[ "$dir" != "/" && ! -f "$dir/.env" ]]; do dir="$(dirname "$dir")"; done
[[ -f "$dir/.env" ]] && { set -a; . "$dir/.env"; set +a; }

VOL="vol-cli-$(date +%s)"
PLABEL="vol-prod-$$"
CLABEL="vol-cons-$$"

echo "==> Creating AzureBlob volume $VOL ..."
aca sandboxgroup volume create --name "$VOL" --type AzureBlob >/dev/null

cleanup() {
  for L in "$PLABEL" "$CLABEL"; do
    ID=$(aca sandbox list -l "name=$L" -o json 2>/dev/null | python -c "import sys,json;d=json.load(sys.stdin);print(d[0]['id'] if d else '')")
    [[ -n "$ID" ]] && aca sandbox delete --id "$ID" >/dev/null 2>&1 || true
  done
  aca sandboxgroup volume delete --name "$VOL" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Producer sandbox..."
aca sandbox create --labels "name=$PLABEL" >/dev/null
PID=$(aca sandbox list -l "name=$PLABEL" -o json | python -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
aca sandbox mount --id "$PID" --volume "$VOL" --path /mnt/shared >/dev/null
aca sandbox exec --id "$PID" -c "echo '{\"answer\":42,\"status\":\"ok\"}' > /mnt/shared/output.json"
echo "    producer wrote to /mnt/shared/output.json"

echo "==> Consumer sandbox..."
aca sandbox create --labels "name=$CLABEL" >/dev/null
CID=$(aca sandbox list -l "name=$CLABEL" -o json | python -c "import sys,json;print(json.load(sys.stdin)[0]['id'])")
aca sandbox mount --id "$CID" --volume "$VOL" --path /mnt/shared >/dev/null
echo "==> Consumer reads:"
aca sandbox exec --id "$CID" -c "cat /mnt/shared/output.json"
