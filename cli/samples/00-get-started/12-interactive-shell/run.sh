#!/usr/bin/env bash
# Interactive shell - boot a sandbox, drop into `aca sandbox shell`,
# delete the sandbox on exit.
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

echo "==> Creating sandbox..."
CREATE_OUTPUT="$(aca sandbox create --disk ubuntu)"
echo "$CREATE_OUTPUT"
SANDBOX_ID="$(echo "$CREATE_OUTPUT" | sed -n 's/^Created sandbox: //p' | tail -n1)"
if [[ -z "$SANDBOX_ID" ]]; then
    echo "error: could not parse sandbox id from create output" >&2
    exit 1
fi

cleanup() {
    echo
    echo "==> Shell closed. Deleting sandbox $SANDBOX_ID..."
    aca sandbox delete --id "$SANDBOX_ID" --yes >/dev/null || true
    echo "==> Done."
}
trap cleanup EXIT

echo "==> Opening interactive shell. Type 'exit' (or Ctrl-D) to leave."
# `aca sandbox shell` takes over the TTY. We deliberately don't pipe its
# stdin/stdout so the user gets a real interactive session.
aca sandbox shell --id "$SANDBOX_ID"
