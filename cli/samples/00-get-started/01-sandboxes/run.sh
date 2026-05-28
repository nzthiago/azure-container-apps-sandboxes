#!/usr/bin/env bash
# ----- Getting started - create a sandbox, run a command, delete it (aca CLI).
#
# Three flavors back-to-back:
#   1. Basic    - aca sandbox create --disk ubuntu   (everything else default)
#   2. Advanced - explicit --cpu / --memory / --env / --label flags
#   3. YAML     - aca sandbox apply --file sandbox.yaml (declarative spec)
#
# The YAML flow is the same shape as advanced but expressed as a spec file
# you can check in next to your repo - useful for CI/CD and reviewing
# sandbox config alongside source.
#
# Defaults applied when a flag is omitted (service-side, matches SDK):
#   --cpu        1000m  (1 vCPU)
#   --memory     2048Mi (2 GiB)
#   auto-suspend 300s   (5 min idle -> suspend; no CLI flag, group default)
#   --env        (none)
#   --label      (none)
#
# Reads samples/.env (written by samples/sandboxes/setup/cli/setup.sh) for
# ACA_SUBSCRIPTION, ACA_RESOURCE_GROUP, ACA_SANDBOX_GROUP.

set -euo pipefail

# Walk up from this script to find samples/.env.
dir="$(cd "$(dirname "$0")" && pwd)"
while [[ "$dir" != "/" && ! -f "$dir/.env" ]]; do
    dir="$(dirname "$dir")"
done
if [[ -f "$dir/.env" ]]; then
    set -a
    # shellcheck disable=SC1091
    . "$dir/.env"
    set +a
else
    echo "error: could not find samples/.env - run setup/cli/setup.sh first?" >&2
    exit 1
fi

BASIC_ID=""
ADVANCED_ID=""
YAML_ID=""

cleanup() {
    if [[ -n "$BASIC_ID" ]]; then
        echo "==> Deleting basic sandbox $BASIC_ID..."
        aca sandbox delete --id "$BASIC_ID" --yes >/dev/null || true
    fi
    if [[ -n "$ADVANCED_ID" ]]; then
        echo "==> Deleting advanced sandbox $ADVANCED_ID..."
        aca sandbox delete --id "$ADVANCED_ID" --yes >/dev/null || true
    fi
    if [[ -n "$YAML_ID" ]]; then
        echo "==> Deleting yaml sandbox $YAML_ID..."
        aca sandbox delete --id "$YAML_ID" --yes >/dev/null || true
    fi
}
trap cleanup EXIT

parse_id() {
    sed -n 's/^Created sandbox: //p' | tail -n1
}

# ----- Basic create (all defaults) -----
echo "==> Creating basic sandbox (defaults)..."
CREATE_OUTPUT="$(aca sandbox create --disk ubuntu)"
echo "$CREATE_OUTPUT"
BASIC_ID="$(echo "$CREATE_OUTPUT" | parse_id)"
if [[ -z "$BASIC_ID" ]]; then
    echo "error: could not parse sandbox id from basic create output" >&2
    exit 1
fi

echo "--- basic exec ---"
aca sandbox exec --id "$BASIC_ID" -c "echo hello world && uname -a"

# ----- Advanced create (override common knobs) -----
echo "==> Creating advanced sandbox (explicit cpu/memory/env/labels)..."
CREATE_OUTPUT="$(aca sandbox create \
    --disk ubuntu \
    --cpu 2000m \
    --memory 4096Mi \
    --env 'GREETING=hello from advanced sandbox' \
    --label sample=01-sandboxes \
    --label tier=advanced)"
echo "$CREATE_OUTPUT"
ADVANCED_ID="$(echo "$CREATE_OUTPUT" | parse_id)"
if [[ -z "$ADVANCED_ID" ]]; then
    echo "error: could not parse sandbox id from advanced create output" >&2
    exit 1
fi

echo "--- advanced exec ---"
aca sandbox exec --id "$ADVANCED_ID" -c 'echo $GREETING && nproc && free -m | head -n2'

# ----- list + get (create / list / get convention) -----
echo "==> List sandboxes in this group:"
aca sandbox list

echo "==> Get details for advanced sandbox:"
aca sandbox get --id "$ADVANCED_ID"

# ----- YAML create (apply a checked-in spec file) -----
echo "==> Creating sandbox from sandbox.yaml (aca sandbox apply)..."
SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
echo "--- sandbox.yaml ---"
cat "$SCRIPT_DIR/sandbox.yaml"
echo "---"
CREATE_OUTPUT="$(aca sandbox apply --file "$SCRIPT_DIR/sandbox.yaml")"
echo "$CREATE_OUTPUT"
YAML_ID="$(echo "$CREATE_OUTPUT" | parse_id)"
if [[ -z "$YAML_ID" ]]; then
    echo "error: could not parse sandbox id from apply output" >&2
    exit 1
fi

echo "--- yaml exec ---"
aca sandbox exec --id "$YAML_ID" -c 'echo $GREETING && nproc'

echo "==> Done."
