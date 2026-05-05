Part of [Azure Container Apps Sandboxes](../../README.md).

# Agent Swarms with Durable Task Scheduler and ACA Sandboxes

A FastAPI sample service that runs an **agent swarm** on Azure. The service
plans, executes, reviews, and publishes code changes to a GitHub repository
using **GitHub Copilot**, **Durable Task Scheduler (DTS)**, and **Azure
Container Apps (ACA) Sandboxes**. Deploy it with one `azd up` and drive it from
the built-in Swagger UI at `/api/docs`.

## What you get

- One FastAPI service deployed as an Azure Container App.
- **DTS** orchestrates each swarm run end-to-end (plan → work → review → publish).
- **Azure Storage** holds the run-id index and short-lived run-scoped GitHub secrets.
- **ACA Sandboxes** execute the planner, worker, and reviewer roles (one per task) and stream their logs.
- A clean public HTTP surface: `/api/health` and `/api/swarm-runs/...`.
- Swagger UI at `/api/docs` (with the OpenAPI document at `/api/openapi.json`) so you can create and inspect runs without writing a client.

## How it works (at a glance)

```
   +------------+    POST /api/swarm-runs    +-----------------+
   |  Caller    +--------------------------> |   FastAPI app   |
   | (you, curl,|    (prompt + GitHub PAT)   | (Container App) |
   |  Swagger)  | <--+   run id, status      +--------+--------+
   +------------+    |                                |
                     |                                v
                     |                       +-----------------+
                     |                       |   DTS (orches-  |
                     |                       |   trates plan/  |
                     |                       |   work/review)  |
                     |                       +--------+--------+
                     |                                |
                     |                                v
                     |                       +-----------------+
                     |                       |  ACA Sandboxes  |
                     |                       |  (one per task, |
                     |                       |  Copilot SDK)   |
                     |                       +--------+--------+
                     |                                |
                     |   PR opened/updated            v
                     +-----------------------> +-----------------+
                                               |  GitHub repo    |
                                               +-----------------+
```

## Prerequisites

You need these on your workstation:

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or any local Docker engine that `azd` can use to build the container image)
- Git

You also need:

- An Azure subscription with rights to create resource groups, role
  assignments, and the resources listed under
  [What `azd up` provisions](#what-azd-up-provisions).
- A **GitHub personal access token (PAT)** with:
  - Read/write access to the target repository (so the swarm can branch and open pull requests).
  - The **Copilot Requests** scope. This requires a GitHub account with
    [GitHub Copilot](https://github.com/features/copilot) enabled (Copilot
    Pro, Business, or Enterprise — see GitHub's docs for current eligibility).

## Deploy

### 1. Clone the repo and sign in

```powershell
# Clone this repo, then:
cd samples/agent-swarms

az login
azd auth login
```

### 2. Create an azd environment

```powershell
azd env new <environment-name>
azd env set AZURE_LOCATION <supported-region>
```

`AZURE_LOCATION` must be a region that supports both **ACA Sandboxes**
(currently in preview) and **Durable Task Scheduler**. If you are not sure,
start with one of the public preview regions for ACA Sandboxes — the
preprovision hooks register the required resource providers for you.

### 3. First `azd up`

```powershell
azd up
```

This provisions every Azure resource (including the sandbox group and the
Azure Container Registry), builds the container image from the repo-root
`Dockerfile`, pushes it to ACR, and rolls out the Container App.

> **Note:** If `azd provision` succeeds but the Container App is still on the
> placeholder image, run `azd deploy` once more to push the freshly built
> image and roll out the real revision.

At this point the swarm service is running, but `POST /api/swarm-runs` will
fail with *"A private sandbox DiskId is required"* until you complete steps
4 and 5. That is expected — sandbox disk images live **inside** the sandbox
group, so the group has to exist before you can create one.

### 4. Build and register the sandbox disk image

ACA Sandboxes execute every planner/worker/reviewer task inside a private
disk image scoped to **your** sandbox group. The repo ships a compatible
sample image at [`sandbox-image/`](sandbox-image/README.md).
The full step-by-step (build → push to ACR → register against the sandbox
group via SDK or portal) lives in that README. The short version is:

1. Build the image locally and push it to the ACR that `azd up` created.
2. Register that container image as a disk image inside your sandbox group.
   You have two options:
   - **ACA Sandboxes preview portal:** open
     `https://staging.containerapps.azure.com/sandbox-groups/{subscriptionId}/{resourceGroup}/{sandboxGroupName}/disk-images`
     (substitute the values from `azd env get-values`) and create a new
     disk image pointing at the ACR image you just pushed. ACA Sandboxes
     don't have a `portal.azure.com` blade yet while the feature is in
     preview.
   - **Python SDK:** call `azure.containerapps.sandbox.SandboxClient.create_disk_image(...)`
     against your sandbox group. See the sample README for a copy-pasteable
     snippet.

Both paths return a disk image whose `id` is what the service expects in
`SWARM_SANDBOX_DISK_ID`.

### 5. Wire the DiskId into the deployment and re-provision

```powershell
azd env set SWARM_SANDBOX_DISK_ID "<the-disk-image-id-from-step-4>"
azd up
```

> **Important:** use `azd up` (or `azd provision` followed by `azd deploy`),
> **not** `azd deploy` alone. `SWARM_SANDBOX_DISK_ID` is injected into the
> Container App's env vars by `infra/main.bicep` during *provisioning*, so a
> deploy-only step will not pick it up and `POST /api/swarm-runs` will keep
> failing with *"A private sandbox DiskId is required"*.

You can verify the env var actually landed on the Container App with:

```powershell
$envName = (azd env get-values | Select-String '^containerAppName=').ToString().Split('=')[1].Trim('"')
$rg = (azd env get-values | Select-String '^AZURE_RESOURCE_GROUP=').ToString().Split('=')[1].Trim('"')
az containerapp show --name $envName --resource-group $rg --query "properties.template.containers[0].env[?name=='SWARM_SANDBOX_DISK_ID']"
```

The output should include the disk image id you set. If the array is empty,
the env var did not propagate — re-run `azd up`.

After the provision step succeeds, every `POST /api/swarm-runs` (without an
explicit `options.sandboxDiskId`) uses this image.

You can also skip steps 4 and 5 entirely and instead supply
`options.sandboxDiskId` per run on `POST /api/swarm-runs`. Setting a
deployment default is just more convenient when you intend to use the same
image for most runs.

### 6. Verify the deploy

```powershell
azd env get-values
```

Copy the `containerAppUrl` value, then in a browser or with `curl`:

- `<containerAppUrl>/api/health` → returns `{ "status": "healthy", ... }` (the same handler is also exposed at `/health` for the Container App's liveness/readiness probes).
- `<containerAppUrl>/api/docs` → opens the Swagger UI for the swarm-runs API.

## Run your first swarm

In Swagger UI:

1. Expand `POST /api/swarm-runs`.
2. Click **Try it out**.
3. Submit a request like the one below and click **Execute**.

```json
{
  "prompt": "Add a short quickstart section to the README",
  "repositoryUrl": "https://github.com/octo-org/octo-repo",
  "githubPat": "github_pat_your_token_here",
  "baseBranch": "main"
}
```

Optional per-run sandbox override:

```json
{
  "prompt": "Add a short quickstart section to the README",
  "repositoryUrl": "https://github.com/octo-org/octo-repo",
  "githubPat": "github_pat_your_token_here",
  "baseBranch": "main",
  "options": {
    "sandboxDiskId": "<the-disk-image-id>"
  }
}
}
```

What happens next:

- The service stores your PAT as a short-lived secret keyed by the new run id.
- DTS schedules the run and walks it through plan → work → review → publish.
- ACA Sandboxes execute each step with the Copilot SDK, using your PAT just in time.
- The PAT is never returned in API responses and is never persisted in DTS state.

You can poll progress, list events, or stream logs from the same Swagger UI:

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/swarm-runs` | List runs the service knows about |
| `GET` | `/api/swarm-runs/{runId}` | Run summary |
| `GET` | `/api/swarm-runs/{runId}/plan` | Current plan |
| `GET` | `/api/swarm-runs/{runId}/tasks` | Task list and status |
| `GET` | `/api/swarm-runs/{runId}/details` | Full run details (reviews, branches, PR) |
| `GET` | `/api/swarm-runs/{runId}/events` | Server-sent event stream of run state |
| `GET` | `/api/swarm-runs/{runId}/sandboxes/{sandboxId}/logstream` | Live log tail for an active sandbox |
| `POST` | `/api/swarm-runs/{runId}/suspend` / `resume` / `rerun` | Lifecycle controls |
| `DELETE` | `/api/swarm-runs/{runId}` | Cancel |
| `DELETE` | `/api/swarm-runs/{runId}/purge` | Purge state |

## Token handling

- `githubPat` is required on every `POST /api/swarm-runs` and is treated as a secret.
- It is stored server-side as a short-lived per-run secret in Azure Storage.
- It is never returned in API responses and is never written into DTS state.
- The service injects it into sandbox work just in time as `GH_TOKEN` and
  `GITHUB_TOKEN`. `COPILOT_GITHUB_TOKEN` is only injected when the runtime is
  explicitly configured to request it.
- There is no deployment-scoped Copilot fallback token — every run brings its own.
- Any caller with a run id can list, read, and control that run; there are no
  cookies or browser sessions involved.

## What `azd up` provisions

`azure.yaml` and `infra/main.bicep` create:

- An Azure Container Registry.
- An Azure Container Apps managed environment and one Container App.
- A user-assigned managed identity with the role assignments the app needs.
- A Durable Task Scheduler and a task hub.
- A Storage account (used for the run-id index and run-scoped secrets).
- Log Analytics + Application Insights.
- An ACA sandbox group resource.

The container image bundles `azure-containerapps-sandbox` preview SDK
(vendored under `vendor/wheels/`) and installs `github-copilot-sdk` plus `git`
so the deployed app can drive ACA sandboxes and run the Copilot SDK directly.

## Default models

The deployment defaults to `gpt-4.1` for the planner, worker, and reviewer
agents, with no reasoning-effort hint. You can override these per deployment
or per request:

| Scope | Mechanism | Keys |
| --- | --- | --- |
| Deployment default | Edit `infra/main.bicep` params (`defaultPlannerModel`, `defaultWorkerModel`, `defaultReviewerModel`) and re-run `azd up` | The bicep template translates these params into `SWARM_PLANNER_MODEL` / `SWARM_WORKER_MODEL` / `SWARM_REVIEWER_MODEL` env vars on the Container App during *provisioning*. |
| Per-run override | `options` on `POST /api/swarm-runs` | `plannerModel`, `workerModel`, `reviewerModel` |

> Note: `azd env set SWARM_PLANNER_MODEL=...` by itself **does not** change
> the model on the Container App — those env vars are populated by the bicep
> template, not directly from your azd env. Use the bicep params (and re-run
> `azd up`) for deployment defaults, or the per-run options for one-off runs.

Pick models that the GitHub Copilot account behind the PAT is licensed to use.

## Per-run options

`POST /api/swarm-runs` accepts an `options` object with the following fields.
Anything else (typos, removed fields) is rejected so you don't get silent
fallthrough:

| Field | Default | Purpose |
| --- | --- | --- |
| `sandboxDiskId` | from `SWARM_SANDBOX_DISK_ID` | Override the deployed default sandbox image for this run only. |
| `plannerModel` / `workerModel` / `reviewerModel` | `gpt-4.1` (each) | Use a different Copilot model for one of the three agents. |
| `humanReviewMode` | `None` | Set to `Required` to pause for human plan approval. |
| `planReviewTimeoutHours` | `24` | Only applies when `humanReviewMode=Required`. Hours to wait before timing the plan out. |

Operator knobs that used to be settable per-request (sandbox idle timeout,
keep-failed sandboxes, fix-chain depth, replan caps) are still tunable at
deploy time via `SWARM_SANDBOX_IDLE_TIMEOUT_SECONDS`,
`SWARM_KEEP_FAILED_SANDBOXES`, `SWARM_MAX_FIX_CHAIN_DEPTH`, and
`SWARM_MAX_REPLANS` — see `infra/main.bicep` for the full list of
environment variables the Container App receives.

## Local development

The deployed `azd up` flow does not need `azure-containerapps-sandbox` or
`github-copilot-sdk` on your workstation — they are bundled into the container
image. If you want to run the FastAPI app locally for iteration:

```powershell
python -m venv .venv
.\.venv\Scripts\Activate.ps1
python -m pip install --find-links vendor/wheels -e .
python -m uvicorn agent_swarm_service.app:create_runtime_app --factory --reload
```

Local runs require the same environment variables the deployed app receives
(see `infra/main.bicep` for the full list). The simplest path is to run
`azd env get-values` against an existing deployment and import those values
into your shell.

## Tests

```powershell
python -m pytest -q
```

The test suite exercises request validation, route behavior, DTS-backed run
state, and the ACA sandbox integration adapters.

## Cost and cleanup

`azd up` creates Azure resources that cost real money. When you are done,
tear them down:

```powershell
azd down
```

Add `--force --purge` if you want the cleanup to skip prompts and remove soft-deletable resources.

## Learn more

- [Quickstart](docs/quickstart.md) — opinionated step-by-step deploy.
- [Architecture](docs/architecture.md) — how the pieces fit together.
- [Troubleshooting](docs/troubleshooting.md) — common deploy and run issues.
- [`sandbox-image/`](sandbox-image/README.md) — a compatible private sandbox image to use with `SWARM_SANDBOX_DISK_ID`.

## License

Released under the [MIT License](../../LICENSE.md).
