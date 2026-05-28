"""Files - write, read, stat, list, mkdir, delete inside a sandbox.

Reads configuration from samples/.env (written by samples/sandboxes/setup/python/setup.py).
"""

from __future__ import annotations

import os
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

    sandbox = None
    try:
        print("==> Creating sandbox...")
        sandbox = client.begin_create_sandbox(disk="ubuntu").result()
        print(f"    sandbox: {sandbox.sandbox_id}")

        print("==> write_file(/tmp/hello.txt)")
        sandbox.write_file("/tmp/hello.txt", "Hello from the SDK!")

        print("==> read_file(/tmp/hello.txt)")
        content = sandbox.read_file("/tmp/hello.txt")
        print(f"    -> {content.decode()!r}")

        print("==> stat_file(/tmp/hello.txt)")
        stat = sandbox.stat_file("/tmp/hello.txt")
        print(f"    size={stat.size} bytes, is_directory={stat.is_directory}")

        print("==> mkdir(/tmp/demo-dir)")
        sandbox.mkdir("/tmp/demo-dir")

        print("==> list_files(/tmp)")
        listing = sandbox.list_files("/tmp")
        entries = getattr(listing, "entries", listing)
        for entry in entries:
            kind = "DIR " if getattr(entry, "is_directory", False) else "FILE"
            name = getattr(entry, "name", entry)
            print(f"    {kind} {name}")

        print("==> delete_file(/tmp/hello.txt) and delete_file(/tmp/demo-dir)")
        sandbox.delete_file("/tmp/hello.txt")
        sandbox.delete_file("/tmp/demo-dir", recursive=True)

        print("==> Done.")
    finally:
        if sandbox is not None:
            print(f"==> Deleting sandbox {sandbox.sandbox_id}...")
            sandbox.delete()
        client.close()
        credential.close()


if __name__ == "__main__":
    main()
