# Durable Task Workflows + Sandbox Jobs

Use Durable Task Scheduler (DTS) to orchestrate Azure Container Apps sandbox jobs from Python.

## What This Lab Demonstrates

- DTS orchestrates sandbox lifecycle work with Azure Container Apps sandboxes.
- The Durable Task worker runs **outside** the sandbox by default in the notebook kernel or `main.py` process.
- Activities use the `azure.sandbox` and `azure.mgmt.sandbox` clients to:
  - create a sandbox group and sandbox
  - write a small workload into the sandbox
  - execute the workload and capture output
  - collect sandbox stats
  - create a snapshot
  - optionally stop and resume the sandbox
  - optionally clean up the sandbox
- DTS infrastructure is provisioned with the official `az durabletask` CLI extension.
- `durabletask-azuremanaged` stays a separate optional dependency for this lab and is **not** added to the core sandbox SDK package metadata.

## Files

| File | Purpose |
|------|---------|
| `01-orchestrate-sandbox-jobs.ipynb` | Notebook-first walkthrough |
| `main.py` | Script mirror with `argparse` |
| `README.md` | Scenario overview and run instructions |

## Prerequisites

- Azure CLI: `az login`
- Install the sandbox SDK from PyPI, then install DTS separately:

  ```powershell
  pip install azure-sandbox azure-mgmt-sandbox
  pip install durabletask-azuremanaged
  ```

- Or install the sandbox SDK wheels from a GitHub Release for **this repo**, then install DTS separately:

  GitHub CLI is required for this flow: `gh auth status`

  ```powershell
  $wheelDir = Join-Path (Get-Location) '.artifacts\release-wheels'
  New-Item -ItemType Directory -Force -Path $wheelDir | Out-Null

  gh release download <tag> --repo Azure-Samples/azure-container-apps-sandboxes --pattern 'azure_sandbox-*.whl' --dir $wheelDir
  gh release download <tag> --repo Azure-Samples/azure-container-apps-sandboxes --pattern 'azure_mgmt_sandbox-*.whl' --dir $wheelDir

  $sdkWheels = Get-ChildItem "$wheelDir\azure_sandbox-*.whl", "$wheelDir\azure_mgmt_sandbox-*.whl" | ForEach-Object FullName
  pip install @sdkWheels
  pip install durabletask-azuremanaged
  ```

- If `vendor\wheels` exists locally, `main.py` can load those sandbox wheels at runtime as a validation or fallback path. That local vendored copy is **not** the primary install story.
- The lab provisions the official `durabletask` Azure CLI extension automatically, but you can also install it up front with `az extension add --name durabletask`
- If a notebook kernel or managed environment can't find Azure CLI on Windows, restart VS Code or set `AZURE_CLI_PATH` to your Azure CLI executable (for example `C:\\Program Files (x86)\\Microsoft SDKs\\Azure\\CLI2\\wbin\\az.cmd`).

## CLI Boundary

The lab uses the official Durable Task CLI for DTS infrastructure and keeps the sandbox CLI surface separate:

| Job | CLI to use | Typical commands |
|---|---|---|
| Create, show, wait on, or delete DTS schedulers and task hubs | Official `az durabletask` extension | `az durabletask scheduler create ...`<br>`az durabletask taskhub create ...` |
| Manage sandbox groups, sandboxes, files, ports, snapshots, images, volumes, and secrets | `az sandbox` | `az sandboxgroup create ...`<br>`az sandbox snapshot list ...` |


## How to Run

| Mode | How | Best for |
|------|-----|----------|
| **Notebook** | Open `01-orchestrate-sandbox-jobs.ipynb` in VS Code | Step-by-step walkthrough |
| **Script** | `python .\labs\02-durable-task-workflows\main.py --stop-and-resume` | Fast end-to-end run |

Useful script flags:

```powershell
python .\labs\02-durable-task-workflows\main.py `
  --assign-current-user-role `
  --stop-and-resume `
  --cleanup-sandboxes
```

## Workflow Shape

The primary orchestration runs one sandbox lifecycle end to end:

1. Ensure the sandbox group exists.
2. Create a sandbox.
3. Write a tiny workload into `/workspace`.
4. Execute the workload.
5. Capture output and sandbox stats.
6. Create a snapshot.
7. Optionally stop and resume the sandbox.
8. Optionally delete the sandbox.

The lab also runs a small fan-out example that starts a few sandbox jobs in parallel.

## Dashboard + Access

After provisioning, the lab prints:

- the DTS endpoint
- the task hub name
- the dashboard entry point: `https://dashboard.durabletask.io/`

If your notebook or script process can't connect to DTS, grant your signed-in identity the **Durable Task Data Contributor** role on the scheduler or task hub. The lab can attempt that assignment with `--assign-current-user-role`, but you need permission to create role assignments.

## Cleanup Notes

Cleanup is intentionally explicit.

- `--cleanup-sandboxes` deletes each sandbox from inside the workflow.
- `--delete-sandbox-group` removes the sandbox group after the run.
- `--delete-dts` deletes the task hub and scheduler after the run.
- `--delete-resource-group` deletes the entire resource group.

If you leave cleanup disabled, you can inspect the orchestration history in the DTS dashboard and revisit the sandboxes manually.
