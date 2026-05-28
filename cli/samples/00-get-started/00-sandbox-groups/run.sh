#!/usr/bin/env bash
# Sandbox group lifecycle - create group, assign role, run sandbox, clean up.
#
# Walks through the full provisioning flow end-to-end so you see every
# step that's normally hidden behind samples/sandboxes/setup/cli/setup.sh:
#
#   1. Create a sandbox group         (aca sandboxgroup create)
#   2. Assign data-owner role         (aca sandboxgroup role create)
#   3. Create a sandbox, exec, delete (aca sandbox create/exec/delete)
#   4. Delete the sandbox group       (aca sandboxgroup delete)
#
# Uses a throwaway group name (guide-00-<short-id>) so it never collides
# with the shared 'ai-apps-samples-group' used by the other guides.
#
# Reads samples/.env (written by samples/sandboxes/setup/cli/setup.sh) for
# ACA_SUBSCRIPTION, ACA_RESOURCE_GROUP, ACA_REGION.

set -euo pipefail

# Walk up to find samples/.env.
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

# Override ACA_SANDBOX_GROUP for this script only so the throwaway group
# is used by every aca command below.
SUFFIX="$(head -c 8 /dev/urandom | od -An -tx1 | tr -d ' \n')"
SUFFIX="${SUFFIX:0:8}"
GROUP_NAME="guide-00-${SUFFIX}"
export ACA_SANDBOX_GROUP="$GROUP_NAME"

REGION="${ACA_REGION:-${ACA_SANDBOXGROUP_REGION:-westus2}}"
echo "==> Subscription:   $ACA_SUBSCRIPTION"
echo "    Resource group: $ACA_RESOURCE_GROUP"
echo "    Region:         $REGION"
echo "    Sandbox group:  $GROUP_NAME  (will be deleted at end)"

SANDBOX_ID=""
GROUP_CREATED=0

cleanup() {
    if [[ -n "$SANDBOX_ID" ]]; then
        echo "==> Deleting sandbox $SANDBOX_ID..."
        aca sandbox delete --id "$SANDBOX_ID" --yes >/dev/null || true
    fi
    if [[ "$GROUP_CREATED" -eq 1 ]]; then
        echo "==> Deleting sandbox group '$GROUP_NAME'..."
        aca sandboxgroup delete --name "$GROUP_NAME" --yes >/dev/null || \
            echo "    warning: group delete failed" >&2
    fi
}
trap cleanup EXIT

# ----- 1. Create the sandbox group -----
echo "==> Creating sandbox group '$GROUP_NAME' in $REGION..."
aca sandboxgroup create --name "$GROUP_NAME" --location "$REGION"
GROUP_CREATED=1

# ----- 1a. List groups in this resource group -----
echo "==> Listing sandbox groups in '$ACA_RESOURCE_GROUP':"
aca sandboxgroup list

# ----- 1b. Get full details for our new group -----
echo "==> Getting details for '$GROUP_NAME':"
aca sandboxgroup get --name "$GROUP_NAME"

# ----- 2. Assign the data-owner role at GROUP scope -----
PRINCIPAL_ID="$(az ad signed-in-user show --query id -o tsv 2>/dev/null || true)"
if [[ -z "$PRINCIPAL_ID" ]]; then
    # Service principals don't have signed-in-user; fall back to az account.
    PRINCIPAL_ID="$(az account show --query user.assignedIdentityInfo -o tsv 2>/dev/null || true)"
fi
if [[ -z "$PRINCIPAL_ID" ]]; then
    echo "error: could not determine your principal id (az login first?)" >&2
    exit 1
fi
echo "==> Assigning 'Container Apps SandboxGroup Data Owner' to $PRINCIPAL_ID..."
aca sandboxgroup role create \
    --name "$GROUP_NAME" \
    --role "Container Apps SandboxGroup Data Owner" \
    --principal-id "$PRINCIPAL_ID" || echo "    (role assignment may already exist)"

# ----- 3. Use the data plane (CLI retries 403s during RBAC propagation) -----
echo "==> Creating sandbox in the new group..."
CREATE_OUTPUT="$(aca sandbox create --disk ubuntu)"
echo "$CREATE_OUTPUT"
SANDBOX_ID="$(echo "$CREATE_OUTPUT" | sed -n 's/^Created sandbox: //p' | tail -n1)"
if [[ -z "$SANDBOX_ID" ]]; then
    echo "error: could not parse sandbox id from create output" >&2
    exit 1
fi

echo "==> Running command in sandbox..."
aca sandbox exec --id "$SANDBOX_ID" -c 'echo hello from $(hostname)'

echo "==> Done."
