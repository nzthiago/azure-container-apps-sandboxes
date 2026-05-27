# 01 - Sandboxes (Python)

```bash
# One-time, from samples/sandboxes/setup/python/:  python setup.py
pip install -r requirements.txt
python sandboxes.py
```

## What this shows

| API | What it does |
|---|---|
| `SandboxGroupClient` | Sync data-plane client, region-scoped |
| `begin_create_sandbox(disk="ubuntu")` | Boot from a public disk image |
| `sandbox.exec(cmd)` | Run a shell command, get stdout/stderr/exit code |
| `client.list_sandboxes()` / `client.get_sandbox(id)` | Inspect what's running |
| `sandbox.delete()` | Tear down (called in `finally`) |
| `azure.containerapps.sandbox.aio.SandboxGroupClient` | Async sibling — same surface, all coroutines |
| `asyncio.gather(*(client.begin_create_sandbox(...) for _ in range(N)))` | Boot N sandboxes concurrently |

The script runs basic → advanced (sync), then a small async fan-out that
boots three sandboxes side-by-side and execs on each. The async section
imports `from azure.containerapps.sandbox.aio import SandboxGroupClient`
and `from azure.identity.aio import DefaultAzureCredential` — that's the
whole switch.
