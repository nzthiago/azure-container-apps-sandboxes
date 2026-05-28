# 01 - Sandboxes

A sandbox is a fresh, isolated VM you can spin up in seconds, run any
shell command on, and throw away. This guide walks the four ways you'll
do that day to day:

1. **Basic** - `disk="ubuntu"` and nothing else. Shows you what defaults
   you get for free.
2. **Advanced** - the same call with `cpu`, `memory`,
   `auto_suspend_seconds`, `labels`, `environment` set explicitly, so
   you see how to override them.
3. **Parallel** (Python) - boot three sandboxes side-by-side via the
   async client and `asyncio.gather`. The shape you reach for any time
   work fans out: parallel tests, per-task workers, batch evaluation.
4. **YAML** (CLI) - the same advanced create expressed as a spec file
   and applied with `aca sandbox apply --file sandbox.yaml`. The form
   you check into a repo so sandbox config sits next to source and
   shows up in code review.

Choose your style:

- [`python/`](python/) - Python SDK (basic + advanced + parallel in one script)
- [`cli/`](cli/) - `aca` CLI (basic + advanced + YAML apply in one script)

Both variants read configuration from `samples/.env`, which is created
by running [`../../setup/python/setup.py`](../../setup/python/) **or**
[`../../setup/cli/setup.sh`](../../setup/cli/) - pick one. Run it once
before any guide.

## Defaults

What the basic create accepts implicitly, and how to override each:

| Knob | Default | Python keyword | CLI flag |
| --- | --- | --- | --- |
| Disk image | `ubuntu` | `disk="ubuntu"` | `--disk ubuntu` |
| CPU | `1000m` (1 vCPU) | `cpu="1000m"` | `--cpu 1000m` |
| Memory | `2048Mi` (2 GiB) | `memory="2048Mi"` | `--memory 2048Mi` |
| Auto-suspend | 300 s (5 min idle) | `auto_suspend_seconds=300` | _(group default; no flag)_ |
| Labels | none | `labels={"k": "v"}` | `--label k=v` (repeatable) |
| Environment | none | `environment={"K": "v"}` | `--env K=v` (repeatable) |
| Exposed ports | none | `ports=[...]` | _(see guide 06-ports)_ |
| Egress policy | inherits group | `egress_policy=...` | _(see guide 08-egress)_ |

Other public keywords on `begin_create_sandbox`: `disk_id`,
`snapshot_id`, `preset`, `connections`, `volumes`, `entrypoint`, `cmd`,
`skip_egress_proxy`, `polling_timeout` (300), `polling_interval` (3).

## Parallel create (Python, async)

The same `begin_create_sandbox` call works on the async client:

```python
from azure.containerapps.sandbox.aio import SandboxGroupClient
from azure.identity.aio import DefaultAzureCredential
import asyncio

async def boot(client, i):
    poller = await client.begin_create_sandbox(disk="ubuntu",
                                               labels={"worker": str(i)})
    return await poller.result()

async with AsyncExitStack() as stack:
    credential = await stack.enter_async_context(DefaultAzureCredential())
    client = SandboxGroupClient(endpoint, credential, ...)
    sandboxes = await asyncio.gather(*(boot(client, i) for i in range(3)))
```

Three concurrent boots run in about the same wall-clock time as one. The
Python sample (`python/sandboxes.py`) runs basic + advanced first, tears
those down, then does this fan-out so total live sandboxes stays at 3.

## YAML create (CLI)

`aca sandbox apply --file sandbox.yaml` accepts the same fields as the
SDK keyword args, expressed as a spec file you check in:

```yaml
disk: ubuntu
resources:
  cpu: 2000m
  memory: 4096Mi
environment:
  GREETING: hello from yaml sandbox
labels:
  sample: 01-sandboxes
  tier: yaml
lifecycle:
  autoSuspendPolicy:
    enabled: true
    interval: 600
    mode: Memory
```

Generate a starter template with `aca sandbox init`, validate any file
with `aca sandbox validate --file sandbox.yaml`, and view the full
schema with `aca sandbox schema`. The CLI sample (`cli/run.sh`) runs
basic + advanced flag-based creates and then applies the YAML spec
above.

## What you'll see

```
==> Creating basic sandbox (defaults)...
    sandbox: 91d7...
--- basic exec ---
hello world
Linux ... GNU/Linux
==> Creating advanced sandbox (explicit cpu/memory/env/labels)...
    sandbox: a3f2...
--- advanced exec ---
hello from advanced sandbox
2
              total        used        free      ...
Mem:           3936          ...
==> Deleting basic sandbox 91d7...
==> Deleting advanced sandbox a3f2...
==> Done.
```
