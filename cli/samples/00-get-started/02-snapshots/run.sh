#!/usr/bin/env bash
# Snapshots - capture state, boot a new sandbox from it (aca CLI).

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

SNAP_NAME="getting-started-snap-$$"
SANDBOX_A=""
SANDBOX_B=""
LOCAL_FILE=/tmp/aca-sample-payload.txt

cleanup() {
    for id in "$SANDBOX_B" "$SANDBOX_A"; do
        if [[ -n "$id" ]]; then
            echo "==> Deleting sandbox $id..."
            aca sandbox delete --id "$id" --yes >/dev/null 2>&1 || true
        fi
    done
    echo "==> Deleting snapshot $SNAP_NAME..."
    aca sandboxgroup snapshot delete --selector "name=$SNAP_NAME" >/dev/null 2>&1 || true
    rm -f "$LOCAL_FILE"
}
trap cleanup EXIT

echo "==> Creating sandbox A..."
SANDBOX_A="$(aca sandbox create --disk ubuntu | sed -n 's/^Created sandbox: //p' | tail -n1)"
echo "    A: $SANDBOX_A"

echo "==> Writing /tmp/payload.txt in sandbox A..."
printf 'data-before-snapshot' > "$LOCAL_FILE"
aca sandbox fs write --id "$SANDBOX_A" --path /tmp/payload.txt --file "$LOCAL_FILE"

echo "==> Creating snapshot '$SNAP_NAME'..."
aca sandbox snapshot --id "$SANDBOX_A" --name "$SNAP_NAME"
sleep 5

echo "==> List snapshots in this group:"
aca sandboxgroup snapshot list

echo "==> Get the snapshot we just created:"
aca sandboxgroup snapshot get --selector "name=$SNAP_NAME"

echo "==> Creating sandbox B from snapshot..."
SANDBOX_B="$(aca sandbox create --snapshot "$SNAP_NAME" | sed -n 's/^Created sandbox: //p' | tail -n1)"
echo "    B: $SANDBOX_B"
sleep 15

echo "==> Reading /tmp/payload.txt in sandbox B..."
aca sandbox fs cat --id "$SANDBOX_B" --path /tmp/payload.txt
echo

echo "==> Done."
