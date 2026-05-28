# 11 - Labels & selectors

Tag sandboxes with `labels={...}` at create-time, then find them later
with `list_sandboxes(labels={...})` or `-l name=foo` on the CLI.

This is the standard "fleet management" pattern: tenant-id, agent-role,
job-id, environment — any key-value you need to query on.

- [`python/`](python/) — `begin_create_sandbox(labels={"role": "worker"})` + `list_sandboxes(labels={"role": "worker"})`
- [`cli/`](cli/) — `aca sandbox create --labels role=worker,tenant=t42` + `aca sandbox list -l role=worker`

## What's covered

| API | Python | CLI |
|---|---|---|
| Set labels on create | `begin_create_sandbox(labels={"k": "v"})` | `--labels k=v,k2=v2` |
| Filter by labels (AND) | `list_sandboxes(labels={"k": "v"})` | `list -l k=v` |
| Read labels off a sandbox | `sandbox_obj.labels` | (in `list -o json` output) |

## Why this matters

`name=...` is the de-facto label CLI users always set; that's what `-l`
selector matching keys off. Multi-label is **AND** (all must match).
For free-text or wildcard queries, fetch all + filter in your code.
