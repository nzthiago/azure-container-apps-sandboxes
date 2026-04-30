# Durable Task Workflows

Use Durable Task Scheduler (DTS) when sandbox work needs durable orchestration instead of a one-shot script.

## What This Skill Entry Point Covers

- Script: [durable-task-sandbox-workflows.py](../scripts/durable-task-sandbox-workflows.py)
- Lab notebook: [01-orchestrate-sandbox-jobs.ipynb](https://github.com/Azure-Samples/azure-container-apps-sandboxes/blob/main/labs/02-durable-task-workflows/01-orchestrate-sandbox-jobs.ipynb)
- Lab script: [labs/02-durable-task-workflows/main.py](https://github.com/Azure-Samples/azure-container-apps-sandboxes/blob/main/labs/02-durable-task-workflows/main.py)
- Lab README: [labs/02-durable-task-workflows/README.md](https://github.com/Azure-Samples/azure-container-apps-sandboxes/blob/main/labs/02-durable-task-workflows/README.md)

The skill-side script is a thin entry point into the lab implementation so the docs, orchestration flow, and CLI surface stay aligned.

## Architecture

- The Durable Task worker runs in the notebook or Python process by default, **outside** the sandbox.
- DTS persists orchestration state, timers, history, and custom status in the scheduler + task hub.
- Activities use `azure.sandbox` and `azure.mgmt.sandbox` to create sandbox groups, create sandboxes, stage workloads, execute jobs, capture stats, snapshot, optionally stop/resume, and optionally clean up.
- DTS scheduler and task hub lifecycle stays on the official `az durabletask` CLI extension.
- `durabletask-azuremanaged` remains an optional sample dependency; it is not part of the core sandbox SDK package metadata.

## Install

```powershell
az login
gh auth status

$wheelDir = Join-Path (Get-Location) '.artifacts\release-wheels'
New-Item -ItemType Directory -Force -Path $wheelDir | Out-Null

gh release download <tag> --repo Azure-Samples/azure-container-apps-sandboxes --pattern 'azure_sandbox-*.whl' --dir $wheelDir
gh release download <tag> --repo Azure-Samples/azure-container-apps-sandboxes --pattern 'azure_mgmt_sandbox-*.whl' --dir $wheelDir

$sdkWheels = Get-ChildItem "$wheelDir\azure_sandbox-*.whl", "$wheelDir\azure_mgmt_sandbox-*.whl" | ForEach-Object FullName
python -m pip install @sdkWheels
python -m pip install durabletask-azuremanaged
az extension add --name durabletask
```

If `vendor\wheels` exists locally, the lab implementation can load those sandbox wheels as a local validation or fallback path. The published GitHub Release wheels are still the primary install story.

## Run

```powershell
python .\plugin\skills\azure-sandbox\scripts\durable-task-sandbox-workflows.py --stop-and-resume
```

Useful flags:

- `--assign-current-user-role` - grant the signed-in identity `Durable Task Data Contributor`
- `--cleanup-sandboxes` - delete each sandbox from inside the workflows
- `--skip-fan-out` - run only the primary workflow
- `--delete-sandbox-group` / `--delete-dts` / `--delete-resource-group` - remove retained resources after inspection

## CLI Boundary

| Job | CLI to use |
|---|---|
| Create, wait on, show, or delete DTS schedulers and task hubs | Official `az durabletask` extension |
| Manage sandbox groups, sandboxes, files, ports, snapshots, images, volumes, and secrets | `az sandbox` or the Python sandbox SDK |

This repo intentionally keeps DTS infrastructure lifecycle on `az durabletask` rather than adding sandbox-specific DTS commands.

## RBAC + Dashboard

- `Durable Task Data Contributor` is the simplest role for a notebook or script that both schedules and inspects workflows.
- `--assign-current-user-role` can attempt that assignment for the signed-in user, but you still need permission to create role assignments.
- Dashboard access still depends on Durable Task data-plane RBAC.
- The dashboard entry point is <https://dashboard.durabletask.io/>.

## When to Use DTS

Use DTS when you need:

- long-running or retryable sandbox workflows
- fan-out across multiple sandboxes
- resumability after notebook or script restarts
- workflow history, custom status, or dashboard visibility
- explicit coordination points like timers or external events

Use a simple script when the flow is short, linear, synchronous, and easy to rerun end to end.

## Links

- DTS overview: <https://learn.microsoft.com/azure/durable-task/scheduler/durable-task-scheduler>
- DTS dashboard: <https://learn.microsoft.com/azure/durable-task/scheduler/durable-task-scheduler-dashboard>
- Managed identity + RBAC: <https://learn.microsoft.com/azure/durable-task/scheduler/durable-task-scheduler-identity>
- Durable Task SDK quickstart for Azure Container Apps: <https://learn.microsoft.com/azure/durable-task/sdks/quickstart-container-apps-durable-task-sdk>
