# Quickstart

This is the opinionated, step-by-step path for deploying the sample to your
own Azure subscription with `azd up` and creating your first swarm run.

If you would rather skim, the [README](../README.md) has the same flow in a
more condensed form.

## 1. Prerequisites

Install on your workstation:

- [Azure CLI](https://learn.microsoft.com/cli/azure/install-azure-cli)
- [Azure Developer CLI (`azd`)](https://learn.microsoft.com/azure/developer/azure-developer-cli/install-azd)
- [Docker Desktop](https://www.docker.com/products/docker-desktop/) (or any local Docker engine that `azd` can use to build the image)
- Python 3.11+ (only needed if you also want to run the test suite locally)
- Git

You also need:

- An Azure subscription where you can create:
  - Azure Container Apps (`Microsoft.App`) including the **sandbox group preview** resource.
  - Azure Container Registry (`Microsoft.ContainerRegistry`).
  - Durable Task Scheduler (`Microsoft.DurableTask`).
  - User-assigned managed identities and role assignments on the resource group.
  - Azure Storage and Log Analytics / Application Insights.
- A **GitHub PAT** that has:
  - Read/write access to the repository the swarm will work on (so it can branch and open pull requests).
  - The **Copilot Requests** scope. This is only available on GitHub accounts
    with Copilot enabled (Copilot Pro, Business, or Enterprise — see GitHub's
    docs for current eligibility).

## 2. Register Azure providers (once per subscription)

The preprovision hooks register `Microsoft.App` and `Microsoft.DurableTask`
for you, but registering everything up front avoids partial-failure cycles:

```powershell
az provider register --namespace Microsoft.App
az provider register --namespace Microsoft.ContainerRegistry
az provider register --namespace Microsoft.DurableTask
az provider register --namespace Microsoft.ManagedIdentity
az provider register --namespace Microsoft.Storage
az provider register --namespace Microsoft.OperationalInsights
az provider register --namespace Microsoft.Insights
```

Pick a region that supports both **ACA Sandboxes** (currently a public
preview) and **Durable Task Scheduler**. If you are not sure which to use,
check the Azure docs for the latest list of preview regions for ACA
Sandboxes.

## 3. Clone the repo and sign in

```powershell
# Clone this repo, then:
cd samples/agent-swarms

az login
azd auth login
```

## 4. Create an azd environment

```powershell
azd env new <environment-name>
azd env set AZURE_LOCATION <supported-region>
```

> Do **not** set `SWARM_APP_BASE_URL`, `DTS_CONNECTION_STRING`,
> `AZURE_SUBSCRIPTION_ID`, `AZURE_RESOURCE_GROUP`,
> `SWARM_STORAGE_ACCOUNT_URL`, or `SWARM_SANDBOX_GROUP_NAME` manually — the
> bicep template injects them into the Container App for you.

## 5. First `azd up`

```powershell
azd up
```

`azd up` will:

- Provision all Azure resources from `infra/main.bicep` (including the
  sandbox group and Azure Container Registry).
- Build the container image from the repo-root `Dockerfile` (this bundles
  the preview ACA sandbox SDKs from `vendor/wheels/` and installs
  `github-copilot-sdk` plus `git`).
- Push the image to ACR.
- Roll out the Container App with the right runtime settings injected.

If `azd provision` succeeds but the Container App is still on the placeholder
hello-world image, run `azd deploy` once more to push the freshly built
image and roll out the real revision.

> At this point the swarm service is healthy, but `POST /api/swarm-runs`
> will fail with *"A private sandbox DiskId is required"* until you finish
> steps 6 and 7. That's expected — sandbox disk images live **inside** your
> sandbox group, so the group has to exist first.

## 6. Build and register the sandbox disk image

ACA Sandboxes execute every planner/worker/reviewer task inside a private
disk image scoped to your sandbox group. The repo ships a compatible
sample image at [`sandbox-image/`](../sandbox-image/README.md).
The full step-by-step (build → push to ACR → register against the sandbox
group via SDK or portal) lives in that README. The short version:

1. Build the sample image: `docker build -f sandbox-image/Dockerfile -t agent-swarm-sandbox:latest .`
2. Push it to the ACR `azd up` provisioned (`az acr login`, then `docker tag` and `docker push`).
3. Register the pushed image as a disk image inside your sandbox group, either:
   - **ACA Sandboxes preview portal:** open
     `https://staging.containerapps.azure.com/sandbox-groups/{subscriptionId}/{resourceGroup}/{sandboxGroupName}/disk-images`
     (values come from `azd env get-values`) and create a new disk image
     pointing at the ACR image. ACA Sandboxes don't have a `portal.azure.com`
     blade yet while the feature is in preview.
   - **Python SDK:** call `azure.containerapps.sandbox.SandboxClient.create_disk_image(...)` (the sample README has a copy-pasteable script).
4. Save the returned disk image **id** — that's the value you'll feed into `SWARM_SANDBOX_DISK_ID`.

A compatible sandbox image must include:

- Python 3.11+ (Python 3.12 recommended)
- `git`
- CA certificates for outbound TLS
- `pip`
- `github-copilot-sdk`
- `/bin/sh`
- A writable `/workspace`
- The two baked entry points: `/opt/agent-swarm/run-role.py` and
  `/opt/agent-swarm/copilot_runtime.py` (the sample image takes care of this).

> **Note on the DiskId format.** Sandbox disk images are a *data-plane*
> resource scoped to the sandbox group; they are not an ARM resource type
> and do **not** look like
> `/subscriptions/.../providers/Microsoft.App/diskImages/...`. The value
> the SDK / portal returns is what you paste into `SWARM_SANDBOX_DISK_ID`
> verbatim.

## 7. Wire the DiskId into the deployment and re-provision

```powershell
azd env set SWARM_SANDBOX_DISK_ID "<the-disk-image-id-from-step-6>"
azd up
```

> **Important:** use `azd up` (or `azd provision` followed by `azd deploy`),
> **not** `azd deploy` alone. `SWARM_SANDBOX_DISK_ID` is injected into the
> Container App's env vars by `infra/main.bicep` during *provisioning*, so
> a deploy-only step will not pick it up — `POST /api/swarm-runs` will keep
> failing with *"A private sandbox DiskId is required"*. You can confirm the
> env var landed with:
>
> ```powershell
> $name = (azd env get-values | Select-String '^containerAppName=').ToString().Split('=')[1].Trim('"')
> $rg = (azd env get-values | Select-String '^AZURE_RESOURCE_GROUP=').ToString().Split('=')[1].Trim('"')
> az containerapp show --name $name --resource-group $rg --query "properties.template.containers[0].env[?name=='SWARM_SANDBOX_DISK_ID']"
> ```

Resolution order at run time is always:

1. Per-run request override (`options.sandboxDiskId` on `POST /api/swarm-runs`).
2. Service deployment default (`SWARM_SANDBOX_DISK_ID`).

If neither layer supplies a DiskId, run creation fails before DTS starts.

## 8. Verify the deployment

```powershell
azd env get-values
```

Note the `containerAppUrl` value, then:

- Browse to `<containerAppUrl>/api/health` — you should see a JSON body with
  `"status": "healthy"`. (The same handler is also reachable at `/health`,
  which is what the Container App's liveness/readiness probes use.)
- Browse to `<containerAppUrl>/api/docs` — this is the Swagger UI for the
  swarm-runs API (the underlying OpenAPI document is at `/api/openapi.json`).

## 9. Create your first swarm run

In Swagger UI:

1. Expand `POST /api/swarm-runs`.
2. Click **Try it out**.
3. Paste a request body like the one below and click **Execute**.

```json
{
  "prompt": "Add a short quickstart section to the README",
  "repositoryUrl": "https://github.com/octo-org/octo-repo",
  "githubPat": "github_pat_your_token_here",
  "baseBranch": "main"
}
```

Want a per-run sandbox override? Add `options`:

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
```

What happens server-side:

- The PAT is stored as a short-lived run-scoped secret keyed by run id.
- The run is scheduled in Durable Task Scheduler.
- ACA Sandboxes execute the planner, worker, and reviewer steps. Publishing
  runs in the app/runtime host via the GitHub publisher (not in a sandbox).
- Any caller with the `runId` returned in the response can list, read, and
  control the run later — there are no cookies to share.

## 10. Watch progress

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/swarm-runs` | List runs the service knows about |
| `GET` | `/api/swarm-runs/{runId}` | Run summary |
| `GET` | `/api/swarm-runs/{runId}/plan` | Current plan |
| `GET` | `/api/swarm-runs/{runId}/tasks` | Per-task status |
| `GET` | `/api/swarm-runs/{runId}/details` | Reviews, branches, PR, validation results |
| `GET` | `/api/swarm-runs/{runId}/events` | Server-sent events stream of run state |
| `GET` | `/api/swarm-runs/{runId}/sandboxes/{sandboxId}/logstream` | Live sandbox log tail |
| `POST` | `/api/swarm-runs/{runId}/suspend` / `resume` / `rerun` | Lifecycle controls |
| `DELETE` | `/api/swarm-runs/{runId}` | Cancel |
| `DELETE` | `/api/swarm-runs/{runId}/purge` | Purge state |

## 11. (Optional) Validate the repo locally

```powershell
python -m pytest -q
```

This runs the in-repo test suite that exercises request validation,
DTS-oriented route behavior, and the ACA sandbox integration adapters.

## 12. Clean up

`azd up` creates billable Azure resources. When you are done:

```powershell
azd down
```

Add `--force --purge` to skip prompts and remove soft-deletable resources.

## Stuck?

See [troubleshooting.md](troubleshooting.md) for the most common deploy and
run-time issues.
