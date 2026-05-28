#!/usr/bin/env bash
# Files - write/read/stat/list/mkdir/rm inside a sandbox (aca CLI).

set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
while [[ "$dir" != "/" && ! -f "$dir/.env" ]]; do
    dir="$(dirname "$dir")"
done
if [[ -f "$dir/.env" ]]; then
    set -a; . "$dir/.env"; set +a
else
    echo "error: could not find samples/.env - run setup/cli/setup.sh first?" >&2
    exit 1
fi

echo "==> Creating sandbox..."
CREATE_OUTPUT="$(aca sandbox create --disk ubuntu)"
SANDBOX_ID="$(echo "$CREATE_OUTPUT" | sed -n 's/^Created sandbox: //p' | tail -n1)"
[[ -n "$SANDBOX_ID" ]] || { echo "error: could not parse sandbox id" >&2; exit 1; }
echo "    sandbox: $SANDBOX_ID"

cleanup() {
    echo "==> Deleting sandbox $SANDBOX_ID..."
    aca sandbox delete --id "$SANDBOX_ID" --yes >/dev/null || true
    rm -f /tmp/aca-sample-hello.txt
}
trap cleanup EXIT

# aca sandbox fs write requires a LOCAL file path, so stage one first.
echo "==> aca sandbox fs write /tmp/hello.txt"
printf 'Hello from the CLI!' > /tmp/aca-sample-hello.txt
aca sandbox fs write --id "$SANDBOX_ID" --path /tmp/hello.txt --file /tmp/aca-sample-hello.txt

echo "==> aca sandbox fs cat /tmp/hello.txt"
aca sandbox fs cat --id "$SANDBOX_ID" --path /tmp/hello.txt

echo "==> aca sandbox fs stat /tmp/hello.txt"
aca sandbox fs stat --id "$SANDBOX_ID" --path /tmp/hello.txt

echo "==> aca sandbox fs mkdir /tmp/demo-dir"
aca sandbox fs mkdir --id "$SANDBOX_ID" --path /tmp/demo-dir

echo "==> aca sandbox fs ls /tmp"
aca sandbox fs ls --id "$SANDBOX_ID" --path /tmp

echo "==> aca sandbox fs rm /tmp/hello.txt && /tmp/demo-dir"
aca sandbox fs rm --id "$SANDBOX_ID" --path /tmp/hello.txt
aca sandbox fs rm --id "$SANDBOX_ID" --path /tmp/demo-dir --recursive

echo "==> Done."
