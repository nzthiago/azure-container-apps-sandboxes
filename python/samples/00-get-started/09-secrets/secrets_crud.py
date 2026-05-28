"""Secrets — upsert, list, list_keys, peek, delete (group-scoped).

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

    secret_id = f"demo-{uuid.uuid4().hex[:8]}"

    try:
        print(f"==> upsert_secret({secret_id!r})...")
        client.upsert_secret(secret_id, {"API_KEY": "sk-test-123", "MODEL": "gpt-4"})

        print("==> list_secrets()...")
        names = [s.id for s in client.list_secrets()]
        print(f"    {len(names)} secret(s) in group; demo present = {secret_id in names}")

        print(f"==> list_secret_keys({secret_id!r})...")
        keys = client.list_secret_keys(secret_id)
        print(f"    keys: {keys}")

        print(f"==> peek_secret({secret_id!r})...")
        peek = client.peek_secret(secret_id)
        for k, v in (peek.values or {}).items():
            masked = v[:3] + "***" if v else ""
            print(f"    {k} = {masked}")

        print(f"==> upsert_secret({secret_id!r}) — update value...")
        client.upsert_secret(secret_id, {"API_KEY": "sk-updated-456", "MODEL": "gpt-4o"})
        peek = client.peek_secret(secret_id)
        print(f"    API_KEY now starts with {peek.values['API_KEY'][:6]}...")

        print("==> Done.")
    finally:
        try:
            print(f"==> delete_secret({secret_id!r})...")
            client.delete_secret(secret_id)
        except Exception as exc:
            print(f"    cleanup warning: {exc}")
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
