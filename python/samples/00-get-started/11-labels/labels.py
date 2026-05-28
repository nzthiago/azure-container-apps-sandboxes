"""Labels & selectors — create with labels, filter by labels.

Reads configuration from samples/.env (written by samples/sandboxes/setup/python/setup.py).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region


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

    tenant = f"t-{uuid.uuid4().hex[:6]}"
    created = []

    try:
        print(f"==> Creating 3 sandboxes — 2x role=worker / 1x role=control (tenant={tenant})...")
        for i, role in enumerate(["worker", "worker", "control"]):
            sbx = client.begin_create_sandbox(
                labels={"tenant": tenant, "role": role, "index": str(i)},
            ).result()
            created.append(sbx)
            print(f"    [{i}] role={role} id={sbx.sandbox_id}")

        print(f"\n==> list_sandboxes(labels={{'tenant': '{tenant}', 'role': 'worker'}})...")
        workers = list(client.list_sandboxes(labels={"tenant": tenant, "role": "worker"}))
        print(f"    matched {len(workers)} sandbox(es); expected 2")
        for s in workers:
            print(f"      {s.id}  labels={s.labels}")

        print(f"\n==> list_sandboxes(labels={{'tenant': '{tenant}', 'role': 'control'}})...")
        controls = list(client.list_sandboxes(labels={"tenant": tenant, "role": "control"}))
        print(f"    matched {len(controls)} sandbox(es); expected 1")
    finally:
        for sbx in created:
            try:
                sbx.delete()
            except Exception:
                pass
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
