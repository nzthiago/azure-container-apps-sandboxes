"""Lifecycle — stop, resume, auto-suspend / auto-delete policy.

Reads configuration from samples/.env (written by samples/sandboxes/setup/python/setup.py).
"""

from __future__ import annotations

import os
import time
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import (
    AutoDeletePolicy,
    AutoSuspendPolicy,
    LifecyclePolicy,
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

    sandbox = None
    try:
        print("==> Booting sandbox...")
        sandbox = client.begin_create_sandbox(labels={"guide": "lifecycle"}).result()
        sid = sandbox.sandbox_id
        print(f"    sandbox: {sid}")
        print(f"    state: {client.get_sandbox(sid).state}")

        print("\n==> set_lifecycle_policy(auto_suspend=60s, auto_delete=600s)...")
        sandbox.set_lifecycle_policy(LifecyclePolicy(
            auto_suspend=AutoSuspendPolicy(enabled=True, interval=60, mode="Memory"),
            auto_delete=AutoDeletePolicy(enabled=True, delete_interval_seconds=600),
        ))

        print("\n==> sandbox.stop()...")
        sandbox.stop()
        time.sleep(3)
        print(f"    state: {client.get_sandbox(sid).state}")

        print("\n==> sandbox.resume()...")
        sandbox.resume()
        sandbox.wait_for_running(timeout=120)
        print(f"    state: {client.get_sandbox(sid).state}")

        print("\n==> exec to confirm it's live...")
        r = sandbox.exec("uptime")
        print(f"    {r.stdout.strip()}")

        print("\n==> ensure_running() — no-op when already Running:")
        sandbox.ensure_running()
        print("    ok")
    finally:
        if sandbox is not None:
            print(f"\n==> Deleting sandbox {sandbox.sandbox_id}...")
            sandbox.delete()
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
