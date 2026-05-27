"""Getting started - create a sandbox, run a command, delete it.

Shows three flavors of `begin_create_sandbox`:

1. **Basic** - just `disk="ubuntu"`; every other knob takes its default.
2. **Advanced** - explicit `cpu`, `memory`, `auto_suspend_seconds`,
   `labels`, `environment` to show how to override the defaults.
3. **Parallel** - same call, but three sandboxes booted concurrently with
   the async sibling client and `asyncio.gather`. Useful any time a job
   fans out across many sandboxes (parallel tests, per-task workers,
   batch evaluation).

Configuration comes from samples/.env (written by
samples/sandboxes/setup/python/setup.py).
"""

from __future__ import annotations

import asyncio
import os
import sys
import time
from pathlib import Path

from azure.identity import DefaultAzureCredential
from azure.identity.aio import DefaultAzureCredential as AsyncDefaultAzureCredential
from azure.containerapps.sandbox import (
    SandboxGroupClient,
    endpoint_for_region,
)
from azure.containerapps.sandbox.aio import (
    SandboxGroupClient as AsyncSandboxGroupClient,
)


def _load_env() -> None:
    """Load samples/.env; exit with a friendly error if it isn't there yet."""
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


def _print_exec(label: str, result) -> None:
    print(f"--- {label} ---")
    if result.stdout:
        sys.stdout.write(result.stdout)
        if not result.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if result.stderr:
        sys.stderr.write(result.stderr)
    if result.exit_code != 0:
        sys.exit(f"command exited with code {result.exit_code}")


def _client_kwargs() -> dict:
    return dict(
        subscription_id=os.environ["AZURE_SUBSCRIPTION_ID"],
        resource_group=os.environ["ACA_RESOURCE_GROUP"],
        sandbox_group=os.environ["ACA_SANDBOX_GROUP"],
    )


def _endpoint() -> str:
    return endpoint_for_region(os.environ["ACA_SANDBOXGROUP_REGION"])


def run_sync() -> None:
    """Basic + advanced create using the sync client."""
    credential = DefaultAzureCredential()
    client = SandboxGroupClient(_endpoint(), credential, **_client_kwargs())

    basic = None
    advanced = None
    try:
        # ----------------------------------------------------------------
        # Basic create -- disk=ubuntu and that's it. Every other knob
        # takes its default. Listed here so you know what you're getting:
        #
        #   cpu="1000m"              # 1 vCPU
        #   memory="2048Mi"          # 2 GiB
        #   auto_suspend_seconds=300 # 5 min idle -> suspend
        #   labels=None              # no labels
        #   environment=None         # no extra env vars
        #   connections=None         # no SandboxGroupConnection refs
        #   ports=None               # no exposed ports
        #   egress_policy=None       # inherit group egress policy
        #   polling_timeout=300      # max wait for Running state
        #   polling_interval=3       # seconds between status polls
        # ----------------------------------------------------------------
        print("==> Creating basic sandbox (defaults)...")
        basic = client.begin_create_sandbox(disk="ubuntu").result()
        print(f"    sandbox: {basic.sandbox_id}")
        _print_exec("basic exec", basic.exec("echo hello world && uname -a"))

        # ----------------------------------------------------------------
        # Advanced create -- override the common knobs. Anything not
        # listed still falls back to the defaults shown above.
        # ----------------------------------------------------------------
        print("==> Creating advanced sandbox (explicit cpu/memory/env/labels)...")
        advanced = client.begin_create_sandbox(
            disk="ubuntu",
            cpu="2000m",                       # 2 vCPU
            memory="4096Mi",                   # 4 GiB
            auto_suspend_seconds=600,          # 10 min idle -> suspend
            labels={"sample": "01-sandboxes", "tier": "advanced"},
            environment={"GREETING": "hello from advanced sandbox"},
        ).result()
        print(f"    sandbox: {advanced.sandbox_id}")
        _print_exec(
            "advanced exec",
            advanced.exec("echo $GREETING && nproc && free -m | head -n2"),
        )

        # ----------------------------------------------------------------
        # List + get (the create / list / get convention).
        # ----------------------------------------------------------------
        print("==> list_sandboxes() in this group:")
        for s in client.list_sandboxes():
            marker = ""
            if s.id == basic.sandbox_id:
                marker = "  <-- basic"
            elif s.id == advanced.sandbox_id:
                marker = "  <-- advanced"
            print(f"    - {s.id}  state={s.state or '?'}{marker}")

        print(f"==> get_sandbox({advanced.sandbox_id}):")
        detail = client.get_sandbox(advanced.sandbox_id)
        res = detail.resources
        print(
            f"    cpu={res.cpu if res else '?'}  "
            f"memory={res.memory if res else '?'}  "
            f"labels={detail.labels or '(none)'}"
        )
    finally:
        # Tear these down before the parallel section so total live
        # sandboxes never exceeds 3 (the fan-out size).
        if basic is not None:
            print(f"==> Deleting basic sandbox {basic.sandbox_id}...")
            basic.delete()
        if advanced is not None:
            print(f"==> Deleting advanced sandbox {advanced.sandbox_id}...")
            advanced.delete()
        client.close()
        credential.close()


async def _boot_and_exec(client: AsyncSandboxGroupClient, worker_id: int):
    """Create one sandbox, run a quick command, return (id, output, sandbox)."""
    poller = await client.begin_create_sandbox(
        disk="ubuntu",
        labels={"sample": "01-sandboxes", "tier": "parallel", "worker": str(worker_id)},
        environment={"WORKER_ID": str(worker_id)},
    )
    sandbox = await poller.result()
    result = await sandbox.exec("echo worker $WORKER_ID on $(hostname)")
    return sandbox, result


async def run_parallel(fan_out: int = 3) -> None:
    """Boot `fan_out` sandboxes concurrently, exec on each, tear down."""
    credential = AsyncDefaultAzureCredential()
    client = AsyncSandboxGroupClient(_endpoint(), credential, **_client_kwargs())

    sandboxes: list = []
    try:
        print(f"==> asyncio.gather over {fan_out} concurrent begin_create_sandbox calls...")
        t0 = time.perf_counter()
        results = await asyncio.gather(
            *(_boot_and_exec(client, i) for i in range(fan_out))
        )
        wall = time.perf_counter() - t0
        for sandbox, result in results:
            sandboxes.append(sandbox)
            line = (result.stdout or "").strip()
            print(f"    {sandbox.sandbox_id}: {line}")
        print(f"==> {fan_out} sandboxes booted + exec'd in {wall:.1f}s wall clock")
    finally:
        if sandboxes:
            print(f"==> Deleting {len(sandboxes)} parallel sandboxes...")
            await asyncio.gather(
                *(s.delete() for s in sandboxes), return_exceptions=True
            )
        await client.close()
        await credential.close()


def main() -> None:
    _load_env()
    run_sync()
    print()
    asyncio.run(run_parallel())
    print("==> Done.")


if __name__ == "__main__":
    main()
