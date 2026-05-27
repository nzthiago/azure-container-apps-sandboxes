"""Volumes — AzureBlob producer/consumer pattern.

Reads configuration from samples/.env (written by samples/sandboxes/setup/python/setup.py).
"""

from __future__ import annotations

import os
import uuid
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import (
    AddVolumeMountRequest,
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

    volume_name = f"vol-{uuid.uuid4().hex[:8]}"
    producer = consumer = None

    try:
        print(f"==> create_volume({volume_name!r}, type='AzureBlob')...")
        vol = client.create_volume(volume_name, type="AzureBlob")
        print(f"    name={vol.name}  type={vol.type}")

        print("==> Booting producer...")
        producer = client.begin_create_sandbox(labels={"role": "producer"}).result()
        producer.add_volume_mount(AddVolumeMountRequest(
            volume_name=volume_name, mountpoint="/mnt/shared",
        ))
        producer.exec("echo '{\"answer\":42,\"status\":\"ok\"}' > /mnt/shared/output.json")
        print(f"    producer wrote /mnt/shared/output.json ({producer.sandbox_id})")

        print("==> Booting consumer (different sandbox, same volume)...")
        consumer = client.begin_create_sandbox(labels={"role": "consumer"}).result()
        consumer.add_volume_mount(AddVolumeMountRequest(
            volume_name=volume_name, mountpoint="/mnt/shared",
        ))
        result = consumer.exec("cat /mnt/shared/output.json")
        print(f"    consumer read: {result.stdout.strip()}")
        assert "42" in result.stdout

        print("==> get_volume() — usage info:")
        info = client.get_volume(volume_name)
        print(f"    is_attached={info.is_attached}  usage={info.usage}")
    finally:
        for sbx in (producer, consumer):
            if sbx is not None:
                try:
                    sbx.delete()
                except Exception:
                    pass
        try:
            print(f"==> delete_volume({volume_name!r})...")
            client.delete_volume(volume_name)
        except Exception as exc:
            print(f"    cleanup warning: {exc}")
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
