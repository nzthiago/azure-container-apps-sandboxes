# `sandbox-image`

A reference Docker image you can publish as a private **disk image inside
your sandbox group** so the agent-swarm-service-python sample can run
planner / worker / reviewer tasks against it.

## When to use this

Every planner, worker, and reviewer task in a swarm run executes inside an
ACA Sandbox started from a private disk image. The sample expects the image
to satisfy a small runtime contract (Python 3.11+, `git`, CA certs, the
`github-copilot-sdk` package, and two baked entry points). This image is the
simplest known-good base that satisfies that contract.

You can:

- Use it as-is to get the sample running end-to-end.
- Use it as a starting point for your own customizations (extra tools,
  alternate network profile, etc.).

## What it contains

- `python:3.12-slim` base image.
- `git` and CA certificates for outbound TLS.
- `github-copilot-sdk==0.3.0`.
- A writable `/workspace` directory.
- Two baked entry points the service invokes:
  - `/opt/agent-swarm/run-role.py` — small launcher (`run-role.py` in this folder).
  - `/opt/agent-swarm/copilot_runtime.py` — copied from
    `src/agent_swarm_service/orchestration/copilot_runtime.py` so the image
    stays in lock-step with the deployed service.
- `CMD ["/bin/sh"]` — ACA Sandboxes invoke a specific command per task.

## Important: a sandbox group has to exist first

Disk images are a **data-plane** resource scoped to a specific sandbox
group; they are *not* an ARM resource type and therefore have no
`/subscriptions/.../providers/Microsoft.App/diskImages/...` path. They live
under the sandbox group at:

```
https://management.azuredevcompute.io
  /subscriptions/{sub}/resourceGroups/{rg}
  /sandboxGroups/{sandbox-group}/diskimages
```

So the order of operations is:

1. Run `azd up` once to provision the sandbox group, the Container App, and
   the Azure Container Registry.
2. Build this Docker image and push it to that ACR.
3. Register the pushed image as a disk image inside your sandbox group
   (Azure portal **or** the `azure-containerapps-sandbox` Python SDK).
4. Set `SWARM_SANDBOX_DISK_ID` to the returned disk image ID and re-deploy.

The rest of this README walks through steps 2–4.

## Step 1 — Build the image

Run from `samples/agent-swarms/` (the Dockerfile copies files from `src/` and
from `sandbox-image/`):

```powershell
docker build -f sandbox-image/Dockerfile -t agent-swarm-sandbox:latest .
```

## Step 2 — Push it to your ACR

`azd up` provisions an Azure Container Registry. Get its login server and
push to it:

```powershell
$acr = (azd env get-values | Select-String '^containerRegistryLoginServer=').ToString().Split('=')[1].Trim('"')
az acr login --name ($acr.Split('.')[0])

docker tag agent-swarm-sandbox:latest "$acr/agent-swarm-sandbox:latest"
docker push "$acr/agent-swarm-sandbox:latest"
```

`AZURE_CONTAINER_REGISTRY_ENDPOINT` is also exposed by `azd env get-values`
if you prefer that name.

## Step 3 — Register the image as a sandbox disk image

You can do this via the **ACA Sandboxes preview portal** (point-and-click)
**or** via the `azure-containerapps-sandbox` Python SDK (scriptable /
repeatable). Both end at the same data-plane API.

### Option A — ACA Sandboxes preview portal

ACA Sandboxes don't yet have a blade in the standard `portal.azure.com`
experience. While the feature is in preview, the only portal that surfaces
disk images is the ACA staging portal at
`https://staging.containerapps.azure.com`. The URL pattern is:

```
https://staging.containerapps.azure.com
  /sandbox-groups/{subscriptionId}/{resourceGroup}/{sandboxGroupName}/disk-images
```

Steps:

1. Get the values you need from your azd environment:

   ```powershell
   azd env get-values | Select-String '^(AZURE_SUBSCRIPTION_ID|AZURE_RESOURCE_GROUP|sandboxGroupName)='
   ```

2. Open the URL above with those values substituted in. For example:

   ```
   https://staging.containerapps.azure.com/sandbox-groups/2ac40cf6-193e-4a44-a55b-d7a17bdd5aee/rg-larohra-test-swarm-py-aca-sdk/swarm-larohra-test-swarm-sandbox/disk-images
   ```

3. Click **Create** (or the equivalent "new disk image" action) and:
   - Name the disk image (e.g. `agent-swarm-sandbox`).
   - Point it at the ACR image you pushed in step 2 (e.g.
     `<acr-login-server>/agent-swarm-sandbox:latest`).
   - If your ACR is private (it is, by default in this sample), provide
     the registry credentials — managed identity auth from the sandbox
     group is the easiest path because `azd up` already grants the app's
     identity ACR pull, but you can also use admin credentials or a
     service principal.

4. Submit. Once the disk image lands, copy its **ID** — that's the value
   you'll feed into `SWARM_SANDBOX_DISK_ID`.

> The staging portal URL above is the current preview path while ACA
> Sandboxes are pre-GA; it is expected to move under `portal.azure.com`
> once the feature ships generally.

### Option B — `azure-containerapps-sandbox` Python SDK

The `azure-containerapps-sandbox` package (vendored in this repo at
`vendor/wheels/`) exposes `SandboxClient.create_disk_image(...)`.

Save the snippet below as `register_disk_image.py` next to this README and
run it. If you're running the script outside the sample's editable
environment, then from `samples/agent-swarms/` install the m`azure-containerapps-sandbox`erged SDK plus
`azure-identity` locally first:

`python -m pip install vendor/wheels/azure_containerapps_sandbox-0.1.0b1-py3-none-any.whl azure-identity`

Or activate the repo's venv after running `python -m pip install --find-links vendor/wheels -e .` from
the repo root.

```python
"""Register the locally-built sandbox image as a private disk image."""
from __future__ import annotations

import subprocess

from azure.containerapps.sandbox import SandboxClient

# Pull the values azd already wrote into the env file.
azd_env = dict(
    line.strip().split("=", 1)
    for line in subprocess.check_output(["azd", "env", "get-values"], text=True).splitlines()
    if "=" in line
)


def _strip(value: str) -> str:
    return value.strip().strip('"')


SUBSCRIPTION_ID = _strip(azd_env["AZURE_SUBSCRIPTION_ID"])
RESOURCE_GROUP = _strip(azd_env["AZURE_RESOURCE_GROUP"])
SANDBOX_GROUP = _strip(azd_env["sandboxGroupName"])
ACR_LOGIN = _strip(azd_env.get("containerRegistryLoginServer") or azd_env["AZURE_CONTAINER_REGISTRY_ENDPOINT"])
MANAGED_IDENTITY_RESOURCE_ID = _strip(azd_env.get("managedIdentityResourceId", ""))

BASE_IMAGE = f"{ACR_LOGIN}/agent-swarm-sandbox:latest"

client = SandboxClient(
    resource_group=RESOURCE_GROUP,
    subscription_id=SUBSCRIPTION_ID,
)

created = client.create_disk_image(
    sandbox_group=SANDBOX_GROUP,
    base_image=BASE_IMAGE,
    name="agent-swarm-sandbox",
    # Use the user-assigned managed identity that azd provisioned so the
    # sandbox group can pull from your private ACR. If your ACR is public
    # or you want to use a service-principal/admin credential, drop this
    # and pass `registry_credentials=...` instead.
    managed_identity_resource_id=MANAGED_IDENTITY_RESOURCE_ID or None,
)

print("Disk image created:")
print("  id  :", created.id)
print("  name:", created.labels.get("name"))
```

`create_disk_image` is implemented as a single PUT against the data-plane
API; the relevant SDK signature is:

```python
from azure.containerapps.sandbox import DiskImage, SandboxClient

SandboxClient.create_disk_image(
    sandbox_group: str,
    base_image: str,
    name: str | None = None,
    entrypoint: list[str] | None = None,
    cmd: list[str] | None = None,
    registry_credentials: dict | None = None,
    managed_identity_resource_id: str | None = None,
    resource_group: str | None = None,
) -> DiskImage   # returns the created disk image model; use `.id` / `.labels`
```

If you need to authenticate against a private ACR with a username/password
instead of managed identity, replace the `managed_identity_resource_id`
argument with:

```python
registry_credentials={
    "server": ACR_LOGIN,
    "username": "<acr-username>",
    "password": "<acr-password-or-token>",
}
```

The companion read/list/delete helpers
(`list_disk_images`, `get_disk_image`, `delete_disk_image`) are on the same
client if you want to inspect or clean up your disk images later.

## Step 4 — Wire the DiskId into the service

```powershell
azd env set SWARM_SANDBOX_DISK_ID "<the-disk-image-id-from-step-3>"
azd up
```

> **Use `azd up`, not `azd deploy`.** `SWARM_SANDBOX_DISK_ID` reaches the
> Container App through `infra/main.bicep` during *provisioning*. `azd
> deploy` only rebuilds and pushes the container image; it does **not**
> re-apply the bicep template, so a deploy-only step leaves the env var
> empty and `POST /api/swarm-runs` keeps failing with *"A private sandbox
> DiskId is required"*. If you prefer the explicit two-step form,
> `azd provision` followed by `azd deploy` works the same way.

You can verify the env var actually landed on the Container App with:

```powershell
$name = (azd env get-values | Select-String '^containerAppName=').ToString().Split('=')[1].Trim('"')
$rg = (azd env get-values | Select-String '^AZURE_RESOURCE_GROUP=').ToString().Split('=')[1].Trim('"')
az containerapp show --name $name --resource-group $rg --query "properties.template.containers[0].env[?name=='SWARM_SANDBOX_DISK_ID']"
```

If the array is empty, the env var did not propagate — re-run `azd up`.

After that, every `POST /api/swarm-runs` (without an explicit
`options.sandboxDiskId`) uses this image.

You can also skip the deployment default and instead pass
`options.sandboxDiskId` per request on `POST /api/swarm-runs`:

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

Resolution order is per-run override first, then deployment default. If
neither is supplied, run creation fails before DTS starts.

## Runtime contract this image satisfies

| Requirement | How this image meets it |
| --- | --- |
| Python 3.11+ | `python:3.12-slim` base image. |
| `git` | Installed via `apt-get`. |
| CA certs / outbound TLS | `ca-certificates` + `update-ca-certificates`. |
| `pip` | Upgraded via `python -m pip install --upgrade pip`. |
| `github-copilot-sdk` | Installed via `pip install github-copilot-sdk==0.3.0`. |
| `/bin/sh` | Provided by the base image. |
| Writable `/workspace` | `mkdir -p /workspace && chmod 0777 /workspace`. |
| Baked launcher | `COPY sandbox-image/run-role.py /opt/agent-swarm/run-role.py`. |
| Baked Copilot runtime | `COPY src/agent_swarm_service/orchestration/copilot_runtime.py /opt/agent-swarm/copilot_runtime.py`. |

If you fork this image and deviate from the contract, the swarm service
will not be able to start the corresponding sandbox role.
