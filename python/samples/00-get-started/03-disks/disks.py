"""Disks — every way to create one, and how to boot from it.

Flow A: build a disk image from a public container image (alpine:3.19),
        boot a sandbox from it, verify it's Alpine.
Flow B: 'prime' a running sandbox, commit it to a new disk image,
        boot a fresh sandbox from the commit, verify state was preserved.

For ACR (private) images on Flow A, pass ``registry_credentials=
RegistryCredentials(username=..., password=...)`` or
``managed_identity_resource_id=...`` to ``begin_create_disk_image``.

Reads configuration from samples/.env (written by samples/sandboxes/setup/python/setup.py).
"""

from __future__ import annotations

import os
import time
import uuid
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import SandboxGroupClient, endpoint_for_region


BASE_IMAGE = "docker.io/library/alpine:3.19"


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


def flow_a_build_from_image(client: SandboxGroupClient) -> None:
    """Build a disk image from a public container image, then boot from it."""
    disk_name = f"alpine-build-{uuid.uuid4().hex[:8]}"
    disk_id = None
    sandbox = None
    try:
        # ----- discovery: what disks can `disk=...` accept? -----
        print("\n=== Flow A: build from container image ===\n")
        print("==> Public disk images (valid `disk=...` values):")
        for img in client.list_public_disk_images():
            print(f"    - {img.name}")

        print(f"\n==> begin_create_disk_image({BASE_IMAGE!r}, name={disk_name!r})...")
        print("    (this can take 5-10 minutes the first time)")
        disk = client.begin_create_disk_image(
            BASE_IMAGE, name=disk_name, polling_timeout=900,
        ).result()
        disk_id = disk.id
        print(f"    built: id={disk.id}  state={disk.status.state if disk.status else '?'}")

        # ----- list + get (create / list / get convention) -----
        print("\n==> list_disk_images() — your private disks in this group:")
        for img in client.list_disk_images():
            marker = "  <-- just created" if img.id == disk_id else ""
            print(f"    - {img.id}  name={img.name or img.labels.get('name', '')}{marker}")

        print(f"\n==> get_disk_image({disk_id}):")
        detail = client.get_disk_image(disk_id)
        print(f"    state:  {detail.status.state if detail.status else '?'}")
        print(f"    source: {detail.image.base if detail.image else '?'}")

        print(f"\n==> begin_create_sandbox(disk_id={disk.id})...")
        sandbox = client.begin_create_sandbox(disk_id=disk.id, labels={"guide": "disks-a"}).result()
        print(f"    sandbox: {sandbox.sandbox_id}")

        print("==> Verifying — should be Alpine:")
        r = sandbox.exec("cat /etc/alpine-release")
        print(f"    {r.stdout.strip()}")
    finally:
        if sandbox is not None:
            print(f"==> Deleting sandbox {sandbox.sandbox_id}...")
            try:
                sandbox.delete()
            except Exception as exc:
                print(f"    cleanup warning: {exc}")
        if disk_id is not None:
            print(f"==> Deleting disk image {disk_id}...")
            try:
                client.delete_disk_image(disk_id)
            except Exception as exc:
                print(f"    cleanup warning: {exc}")


def flow_b_commit_running_sandbox(client: SandboxGroupClient) -> None:
    """Prime a sandbox, commit it to a new disk image, boot a clone, verify."""
    disk_name = f"committed-{uuid.uuid4().hex[:8]}"
    disk_id = None
    primer = clone = None
    try:
        print("\n=== Flow B: commit a primed sandbox ===\n")
        print("==> Booting primer sandbox (default disk)...")
        primer = client.begin_create_sandbox(labels={"guide": "disks-b-primer"}).result()
        print(f"    primer: {primer.sandbox_id}")

        print("==> Priming: write /opt/marker.txt (stand-in for `pip install ...`)...")
        primer.exec('mkdir -p /opt && date -u +"baked-at: %Y-%m-%dT%H:%M:%SZ" > /opt/marker.txt')
        r = primer.exec("cat /opt/marker.txt")
        print(f"    {r.stdout.strip()}")

        print(f"\n==> begin_commit(name={disk_name!r})... (5-10 min)")
        disk = primer.begin_commit(name=disk_name, polling_timeout=1200).result()
        disk_id = disk.id
        print(f"    new disk: id={disk.id}  state={disk.status.state if disk.status else '?'}")

        # Delete primer before booting the clone to free its quota slot
        print("==> Deleting primer (no longer needed)...")
        primer.delete()
        primer = None
        time.sleep(5)

        print(f"\n==> Boot a NEW sandbox from disk {disk.id}...")
        clone = client.begin_create_sandbox(disk_id=disk.id, labels={"guide": "disks-b-clone"}).result()
        print(f"    clone: {clone.sandbox_id}")
        time.sleep(8)

        print("==> Verifying /opt/marker.txt survived the commit/boot cycle...")
        content = clone.read_file("/opt/marker.txt")
        text = content.decode() if isinstance(content, (bytes, bytearray)) else content
        print(f"    {text.strip()}")
        assert "baked-at" in text
        print("    [ok] primed state preserved across boots")
    finally:
        for sbx in (primer, clone):
            if sbx is not None:
                try:
                    sbx.delete()
                except Exception:
                    pass
        if disk_id is not None:
            print(f"==> Deleting committed disk {disk_id}...")
            try:
                client.delete_disk_image(disk_id)
            except Exception as exc:
                print(f"    cleanup warning: {exc}")


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
    try:
        flow_a_build_from_image(client)
        flow_b_commit_running_sandbox(client)
        print("\n[done] both flows completed.")
    finally:
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
