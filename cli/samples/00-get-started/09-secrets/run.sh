#!/usr/bin/env bash
set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
while [[ "$dir" != "/" && ! -f "$dir/.env" ]]; do dir="$(dirname "$dir")"; done
[[ -f "$dir/.env" ]] && { set -a; . "$dir/.env"; set +a; }

NAME="demo-cli-$(date +%s)"

cleanup() {
  echo "==> Deleting secret $NAME..."
  aca sandboxgroup secret delete --name "$NAME" >/dev/null 2>&1 || true
}
trap cleanup EXIT

echo "==> Upsert secret $NAME ..."
aca sandboxgroup secret upsert --name "$NAME" --values "API_KEY=sk-test-123,MODEL=gpt-4"

echo "==> List secrets in this group:"
aca sandboxgroup secret list

echo "==> Update the secret..."
aca sandboxgroup secret upsert --name "$NAME" --values "API_KEY=sk-updated-456,MODEL=gpt-4o"

echo "==> Done."
