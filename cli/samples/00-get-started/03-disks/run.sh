#!/usr/bin/env bash
# Disks - every way to create one, and how to boot from it.
#
# Flow A: build a disk image from a public container image (alpine:3.19),
#         boot a sandbox from it, verify it's Alpine.
# Flow B: 'prime' a sandbox, commit to a new disk image, boot a clone,
#         verify the primed state survived.

set -euo pipefail

dir="$(cd "$(dirname "$0")" && pwd)"
while [[ "$dir" != "/" && ! -f "$dir/.env" ]]; do dir="$(dirname "$dir")"; done
[[ -f "$dir/.env" ]] && { set -a; . "$dir/.env"; set +a; }

# Track ids so cleanup catches everything even on early failure.
A_DISK_NAME="alpine-build-$(date +%s)"
A_DID=""; A_SID=""
B_DISK_NAME="committed-$(date +%s)"
B_DID=""; B_PID=""; B_CID=""

cleanup() {
    for sid in "$A_SID" "$B_PID" "$B_CID"; do
        if [[ -n "$sid" ]]; then
            echo "==> Deleting sandbox $sid..."
            aca sandbox delete --id "$sid" --yes >/dev/null 2>&1 || true
        fi
    done
    for did in "$A_DID" "$B_DID"; do
        if [[ -n "$did" ]]; then
            echo "==> Deleting disk image $did..."
            aca sandboxgroup disk delete --id "$did" >/dev/null 2>&1 || true
        fi
    done
}
trap cleanup EXIT

# `aca sandboxgroup disk create` returns JSON; the first `"id"` is the disk id.
parse_disk_id() { sed -n 's/.*"id": *"\([^"]*\)".*/\1/p' | head -n1; }
# `aca sandbox create` / `aca sandbox commit` print `Created sandbox: <id>` /
# `Committed to disk image: <id>` style lines; this regex matches either.
parse_after_colon() { sed -n 's/^[A-Za-z ]*: \([0-9a-f-][0-9a-f-]*\)$/\1/p' | tail -n1; }

# =========================================================================
echo "=== Flow A: build from container image ==="
# =========================================================================

echo "==> Public disk images (valid --disk values):"
aca sandboxgroup disk list-public

echo "==> Building disk image '$A_DISK_NAME' from alpine:3.19 (5-10 min)..."
OUT="$(aca sandboxgroup disk create --image docker.io/library/alpine:3.19 --name "$A_DISK_NAME")"
echo "$OUT"
A_DID="$(echo "$OUT" | parse_disk_id)"
[[ -z "$A_DID" ]] && { echo "error: could not parse disk id" >&2; exit 1; }

echo "==> Listing your private disk images:"
aca sandboxgroup disk list

echo "==> Get details for '$A_DISK_NAME':"
aca sandboxgroup disk get --id "$A_DID"

# Private/custom disks must be referenced by --disk-id; --disk is for
# public images only (see `aca sandboxgroup disk list-public`).
echo "==> Booting sandbox from disk-id $A_DID..."
OUT="$(aca sandbox create --disk-id "$A_DID")"
echo "$OUT"
A_SID="$(echo "$OUT" | parse_after_colon)"
[[ -z "$A_SID" ]] && { echo "error: could not parse sandbox id" >&2; exit 1; }

echo "==> Verifying — should be Alpine:"
aca sandbox exec --id "$A_SID" -c "cat /etc/alpine-release"

echo "==> Tearing down Flow A..."
aca sandbox delete --id "$A_SID" --yes >/dev/null
A_SID=""
aca sandboxgroup disk delete --id "$A_DID" >/dev/null
A_DID=""

# =========================================================================
echo
echo "=== Flow B: commit a primed sandbox ==="
# =========================================================================

echo "==> Booting primer sandbox (default disk)..."
OUT="$(aca sandbox create)"
echo "$OUT"
B_PID="$(echo "$OUT" | parse_after_colon)"
[[ -z "$B_PID" ]] && { echo "error: could not parse primer sandbox id" >&2; exit 1; }

echo "==> Priming: write /opt/marker.txt..."
aca sandbox exec --id "$B_PID" -c "mkdir -p /opt && date -u +'baked-at: %Y-%m-%dT%H:%M:%SZ' > /opt/marker.txt && cat /opt/marker.txt"

echo "==> Committing primer to disk image '$B_DISK_NAME' (5-10 min)..."
OUT="$(aca sandbox commit --id "$B_PID" --name "$B_DISK_NAME")"
echo "$OUT"
# `sandbox commit` returns JSON describing the new disk image.
B_DID="$(echo "$OUT" | parse_disk_id)"
[[ -z "$B_DID" ]] && { echo "error: could not parse committed disk id" >&2; exit 1; }

echo "==> Deleting primer (no longer needed)..."
aca sandbox delete --id "$B_PID" --yes >/dev/null
B_PID=""
sleep 5

echo "==> Boot a NEW sandbox from disk-id $B_DID..."
OUT="$(aca sandbox create --disk-id "$B_DID")"
echo "$OUT"
B_CID="$(echo "$OUT" | parse_after_colon)"
[[ -z "$B_CID" ]] && { echo "error: could not parse clone sandbox id" >&2; exit 1; }
sleep 8

echo "==> Verifying /opt/marker.txt survived the commit/boot cycle..."
aca sandbox exec --id "$B_CID" -c "cat /opt/marker.txt"

echo
echo "[done] both flows completed."
