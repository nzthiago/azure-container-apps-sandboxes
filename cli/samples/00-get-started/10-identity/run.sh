#!/usr/bin/env bash
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
while [[ "$dir" != "/" && ! -f "$dir/.env" ]]; do dir="$(dirname "$dir")"; done
[[ -f "$dir/.env" ]] && { set -a; . "$dir/.env"; set +a; }

NAME="mi-demo-$(date +%s)"

cleanup() {
  echo "==> Deleting temp group $NAME ..."
  aca sandboxgroup delete --name "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Creating temp sandbox group $NAME ..."
aca sandboxgroup create --name "$NAME" --location "$ACA_REGION" >/dev/null

echo "==> identity assign --system-assigned ..."
aca sandboxgroup identity assign --name "$NAME" --system-assigned

echo "==> identity show:"
aca sandboxgroup identity show --name "$NAME"

echo "==> identity remove ..."
aca sandboxgroup identity remove --name "$NAME" >/dev/null

echo "==> identity show after remove:"
aca sandboxgroup identity show --name "$NAME" || true
