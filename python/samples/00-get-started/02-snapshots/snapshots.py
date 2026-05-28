"""Snapshots - capture sandbox state and restore it into a new sandbox.

Reads configuration from samples/.env (written by samples/sandboxes/setup/python/setup.py).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import (
    SandboxGroupClient,
    endpoint_for_region,
)


def _load_env() -> None:
    """Load samples/.env; exit with a friendly error if it isn't there yet."""
    import sys
    for parent in Path(__file__).resolve().parents:
        env = parent / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            break
    if not os.environ.get("ACA_SANDBOXGROUP_REGION"):
        sys.exit(
            "error: samples/.env is missing required keys. Run:\n"
            "       python samples/sandboxes/setup/python/setup.py"
        )


def main() -> None:
    _load_env()
    credential = DefaultAzureCredential()
    client = SandboxGroupClient(
        endpoint_for_region(os.environ["ACA_SANDBOXGROUP_REGION"]),
        credential,
        subscription_id=os.environ["AZURE_SUBSCRIPTION_ID"],
        resource_group=os.environ["ACA_RESOURCE_GROUP"],
        sandbox_group=os.environ["ACA_SANDBOX_GROUP"],
    )

    sandbox_a = None
    sandbox_b = None
    snap_id = None
    try:
        print("==> Creating sandbox A...")
        sandbox_a = client.begin_create_sandbox(disk="ubuntu").result()
        print(f"    A: {sandbox_a.sandbox_id}")

        print("==> Writing /tmp/payload.txt in sandbox A...")
        sandbox_a.write_file("/tmp/payload.txt", "data-before-snapshot")

        print("==> Creating snapshot...")
        snap = sandbox_a.create_snapshot(name="getting-started-snap")
        snap_id = snap.id
        print(f"    snapshot: {snap_id}")
        # Give the snapshot a moment to settle before restoring from it.
        time.sleep(5)

        # ----- list + get (create / list / get convention) -----
        print("==> list_snapshots() in this group:")
        for s in client.list_snapshots():
            marker = "  <-- just created" if s.id == snap_id else ""
            label = s.labels.get("name", "")
            print(f"    - {s.id}  name={label}  size={s.resources}{marker}")

        print(f"==> get_snapshot({snap_id}):")
        detail = client.get_snapshot(snap_id)
        print(f"    sandbox_id={detail.sandbox_id}  created={detail.created_at_utc}")

        print("==> Creating sandbox B from snapshot...")
        sandbox_b = client.begin_create_sandbox(snapshot_id=snap_id).result()
        print(f"    B: {sandbox_b.sandbox_id}")
        # Snapshot-restored sandboxes need a few extra seconds to warm up.
        time.sleep(15)

        print("==> Reading /tmp/payload.txt in sandbox B...")
        content = sandbox_b.read_file("/tmp/payload.txt").decode()
        print(f"    -> {content!r}")
        if content != "data-before-snapshot":
            raise RuntimeError(f"unexpected content: {content!r}")

        print("==> Done.")
    finally:
        for sbx, label in ((sandbox_b, "B"), (sandbox_a, "A")):
            if sbx is not None:
                print(f"==> Deleting sandbox {label} ({sbx.sandbox_id})...")
                try:
                    sbx.delete()
                except Exception as exc:
                    print(f"    warning: {exc}")
        if snap_id is not None:
            print(f"==> Deleting snapshot {snap_id}...")
            try:
                client.delete_snapshot(snap_id)
            except Exception as exc:
                print(f"    warning: {exc}")
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
