# 02 - Snapshots

Freeze a running sandbox into a snapshot, then boot any number of new
sandboxes from that exact point in time. Handy for branching off a
primed environment (deps installed, repo cloned, model warmed up)
without paying the setup cost on every new sandbox, and for rolling
back when an experiment goes sideways.

- [`python/`](python/) - Python SDK
- [`cli/`](cli/) - `aca` CLI (bash)

## What it does

1. Start sandbox A
2. Write `/tmp/payload.txt` inside it
3. `create_snapshot()`
4. Boot sandbox B from that snapshot
5. Read `/tmp/payload.txt` in sandbox B - it's there
6. Clean up both sandboxes and the snapshot
