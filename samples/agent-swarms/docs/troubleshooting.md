# Troubleshooting

Quick fixes for the most common deploy and run-time issues. If you hit
something not covered here, open an issue.

## `azd up` fails while creating DTS, the sandbox group, or related resources

Most likely cause: missing provider registrations or an unsupported region.

Try this:

1. Re-register the providers (the preprovision hooks register the first two,
   but registering everything up front avoids partial failures):

   ```powershell
   az provider register --namespace Microsoft.App
   az provider register --namespace Microsoft.DurableTask
   az provider register --namespace Microsoft.ContainerRegistry
   az provider register --namespace Microsoft.ManagedIdentity
   az provider register --namespace Microsoft.Storage
   az provider register --namespace Microsoft.OperationalInsights
   az provider register --namespace Microsoft.Insights
   ```

2. Confirm DTS is registered in the subscription:

   ```powershell
   az provider show --namespace Microsoft.DurableTask --query registrationState -o tsv
   ```

3. Switch `AZURE_LOCATION` to a region that supports both **ACA Sandboxes**
   (currently in preview) and **Durable Task Scheduler**.

4. Make sure the identity running `azd up` can create role assignments on the
   target resource group (Owner or User Access Administrator on the resource
   group is the easiest path).

## `azd provision` succeeds but the Container App is still on the placeholder image

This happens when the image build/push step did not run or did not roll out
a new revision. Re-run the deploy step:

```powershell
azd deploy
```

Then refresh `<containerAppUrl>/api/health`.

## `<containerAppUrl>/api/health` returns 502 / 503 / a long delay right after deploy

Cold-start. The Container App's first replica needs a moment to start. Wait
30–60 seconds and refresh. If it still fails:

```powershell
az containerapp logs show --name <container-app-name> --resource-group <rg> --follow --tail 200
```

Then refresh `<containerAppUrl>/api/health`.

## `POST /api/swarm-runs` returns 422

The request is missing a required field. The minimum body is:

```json
{
  "prompt": "Your task",
  "repositoryUrl": "https://github.com/octo-org/octo-repo",
  "githubPat": "github_pat_your_token_here"
}
```

`prompt`, `repositoryUrl`, and `githubPat` are all required.

## `POST /api/swarm-runs` returns "Sandbox DiskId is required" (or run creation fails before DTS starts)

The service has no DiskId to use. See
[`sandbox-image/README.md`](../sandbox-image/README.md) for
the full build → push → register flow. Once you have a disk image ID, either:

- Set a deployment default and **re-provision**:

  ```powershell
  azd env set SWARM_SANDBOX_DISK_ID "<the-disk-image-id>"
  azd up
  ```

  > **Use `azd up`, not `azd deploy`.** `SWARM_SANDBOX_DISK_ID` is added
  > to the Container App's env vars by `infra/main.bicep` during
  > *provisioning*, so a deploy-only step won't propagate the value.
  > Confirm the env var landed with:
  >
  > ```powershell
  > $name = (azd env get-values | Select-String '^containerAppName=').ToString().Split('=')[1].Trim('"')
  > $rg = (azd env get-values | Select-String '^AZURE_RESOURCE_GROUP=').ToString().Split('=')[1].Trim('"')
  > az containerapp show --name $name --resource-group $rg --query "properties.template.containers[0].env[?name=='SWARM_SANDBOX_DISK_ID']"
  > ```

- Or supply one per run:

  ```json
  {
    "prompt": "...",
    "repositoryUrl": "...",
    "githubPat": "...",
    "options": {
      "sandboxDiskId": "<the-disk-image-id>"
    }
  }
  ```

> Sandbox disk images are a data-plane resource scoped to your sandbox
> group; the value is **not** an ARM resource ID and does not look like
> `/subscriptions/.../providers/Microsoft.App/diskImages/...`. Paste in
> exactly the `id` value the SDK or portal returned.

## A run starts but immediately fails because the GitHub token is missing or expired

Per-run PATs are short-lived on purpose. Create the run with a fresh PAT
that has:

- Read/write access to the target repository.
- The **Copilot Requests** scope (only available on accounts with GitHub
  Copilot enabled — Copilot Pro, Business, or Enterprise).

If runs keep failing immediately after deploy, re-run `azd up` (or `azd deploy`)
to make sure the Container App has the expected app settings and storage access.

## Publish fails even though plan/work/review succeeded

Likely the PAT does not have what publishing needs. Confirm:

- The token can read **and** write the target repository.
- The token includes the **Copilot Requests** scope.
- The run is still active (per-run secrets are short-lived; long pauses can
  expire the secret before publish runs).

Then create a fresh run with a new PAT and let it complete in one go.

## A run fails with a Copilot model error (e.g., "model not available")

The deployment defaults to `gpt-4.1` for planner/worker/reviewer. If you
have overridden the defaults, make sure the configured model is licensed
to the GitHub account behind the PAT.

To revert to the defaults, restore the bicep param values in
`infra/main.bicep` (`defaultPlannerModel`, `defaultWorkerModel`,
`defaultReviewerModel`) to their committed defaults (`gpt-4.1` for each)
and re-run:

```powershell
azd up
```

> Per-deployment model defaults flow through bicep parameters, not
> standalone `azd env set` keys, so `azd env set SWARM_PLANNER_MODEL=...`
> by itself has no effect — the `SWARM_*_MODEL` env vars on the Container
> App come from the bicep template and are only refreshed by a
> *provisioning* step. If you need to experiment with different models
> without editing infra, use the `plannerModel` / `workerModel` /
> `reviewerModel` per-run options on `POST /api/swarm-runs`.

## `GET /api/swarm-runs/{runId}` returns 404

Run lookups resolve by run id (no cookies). Things to check:

- The `runId` matches the value returned by `POST /api/swarm-runs` exactly.
- The run was not already purged via `DELETE /api/swarm-runs/{runId}/purge`.
- You are calling the deployed `containerAppUrl`, not a stale one.

## `pytest` reports `azure-sandbox` / `azure-mgmt-sandbox` import errors locally

The `azd up` flow does **not** require those SDKs on your workstation — they
are installed into the container image from `vendor/wheels/`. If you are
running the test suite (or the FastAPI app) locally, install them in your
local environment:

```powershell
python -m pip install -e .
```

That installs the project plus its declared dependencies, including the
preview ACA sandbox SDKs (resolved from `vendor/wheels/` via `pyproject.toml`).

## Clean up

```powershell
azd down
```

Add `--force --purge` to skip prompts and remove soft-deletable resources.

## Verify the repo state at any time

```powershell
python -m pytest -q
```
