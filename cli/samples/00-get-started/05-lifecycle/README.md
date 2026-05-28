# 05 - Lifecycle

Three knobs control a sandbox's lifecycle:

- **stop / resume** — manually pause to save cost; resume on demand.
- **AutoSuspendPolicy** — auto-pause after N seconds of inactivity.
- **AutoDeletePolicy** — auto-tombstone after N seconds (cleanup).

Stopped sandboxes keep their disk state but stop billing for compute.
Resume warms them back up in seconds. Auto-suspend is the default for
sandboxes — set it explicitly to change the timeout.

- [`python/`](python/) — `sandbox.stop()`, `sandbox.resume()`, `sandbox.set_lifecycle_policy(...)`
- [`cli/`](cli/) — `aca sandbox stop`, `aca sandbox resume`, `aca sandbox lifecycle set --auto-suspend N`

## What's covered

| API | Python | CLI |
|---|---|---|
| Stop | `sandbox.stop()` (+ `begin_stop()`) | `aca sandbox stop --id $ID` |
| Resume | `sandbox.resume()` (+ `begin_resume()`) | `aca sandbox resume --id $ID` |
| Ensure running | `sandbox.ensure_running()` | (implicit) |
| Auto-suspend after N seconds | `set_lifecycle_policy(LifecyclePolicy(auto_suspend=AutoSuspendPolicy(enabled=True, interval=60)))` | `aca sandbox lifecycle set --auto-suspend 60` |
| Auto-delete after N seconds | `LifecyclePolicy(auto_delete=AutoDeletePolicy(enabled=True, delete_interval_seconds=600))` | (Python only) |
| Read state | `sandbox.get().state` | `aca sandbox get --id $ID` |

## Why this matters

LLM agents are bursty — a sandbox may sit idle between user turns for
minutes or hours. Set a short `auto_suspend` interval (60-300s) to keep
costs low and `resume` on the next invocation; the SDK's
`ensure_running()` does this for you automatically.
