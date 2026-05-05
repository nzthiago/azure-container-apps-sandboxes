# Architecture

This sample is one FastAPI service that orchestrates **agent swarms** on Azure.
Each run plans, executes, reviews, and publishes code changes to a GitHub
repository, with isolation provided by ACA Sandboxes.

## Components at a glance

```
              +------------------------------+
   client --> | FastAPI app (Container App)  |
              |  - /api/health               |
              |  - /api/swarm-runs/...       |
              |  - Swagger UI at /api/docs   |
              +---------------+--------------+
                              |
              +---------------+----------------+
              |                                |
              v                                v
   +-----------------------+    +--------------------------+
   | Durable Task Scheduler|    |   Azure Storage          |
   | - schedules each run  |    | - run-id index           |
   | - drives plan/work/   |    | - short-lived per-run    |
   |   review/publish      |    |   GitHub PAT secrets     |
   | - holds run state     |    +--------------------------+
   +----------+------------+
              |
              v
   +-------------------------+
   |  ACA Sandboxes (one     |
   |  per task, ephemeral)   |
   |  - planner / worker /   |
   |    reviewer / publisher |
   |  - github-copilot-sdk   |
   |  - mirrored log tail    |
   +------------+------------+
                |
                v
        +---------------+
        | GitHub repo   |
        | (branch + PR) |
        +---------------+
```

## Application shape

- Root package: `src/agent_swarm_service/`.
- App entrypoint: `src/agent_swarm_service/app.py`.
- Container CMD: `agent_swarm_service.app:create_runtime_app` (uvicorn factory).
- HTTP framework: FastAPI.

## Public surface

| Method | Path | Purpose |
| --- | --- | --- |
| `GET` | `/api/health` | Liveness/readiness probe response (also exposed at `/health` for the Container App probes). |
| `GET` | `/api/docs` | Built-in Swagger UI for the swarm-runs API (OpenAPI document at `/api/openapi.json`). |
| `*` | `/api/swarm-runs/...` | List, create, inspect, control, stream events for swarm runs. See `src/agent_swarm_service/api/routers/swarm_runs.py` for the full router. |

There are no GitHub OAuth routes, no `/api/me`, and no token-health endpoints.
Every API call addresses a run by its `runId`.

## What `azd up` provisions

`azure.yaml` points to `infra/main.bicep`, which provisions:

- Azure Container Registry.
- Azure Container Apps managed environment + one Container App.
- A user-assigned managed identity with the role assignments the app needs.
- One Durable Task Scheduler + a task hub.
- A Storage account (used for the run-id index and run-scoped secrets).
- Log Analytics + Application Insights.
- One ACA sandbox group resource.

The Container App receives these environment variables at deploy time:

- `SWARM_APP_BASE_URL`
- `DTS_CONNECTION_STRING`
- `AZURE_SUBSCRIPTION_ID`
- `AZURE_RESOURCE_GROUP`
- `AZURE_LOCATION`
- `SWARM_COPILOT_RUNTIME`
- `SWARM_COPILOT_AUTH_MODE`
- `SWARM_COPILOT_TOKEN_ENV_VAR`
- `SWARM_COPILOT_USE_LOGGED_IN_USER`
- `SWARM_STORAGE_ACCOUNT_URL`
- `SWARM_SANDBOX_GROUP_NAME`
- `AZURE_CLIENT_ID`
- (Optionally) `SWARM_SANDBOX_DISK_ID`.

You should not need to set these by hand.

## Request and run flow

1. A client calls `POST /api/swarm-runs` with a `prompt`, `repositoryUrl`, and `githubPat`.
2. The service:
   - Stores the PAT as a short-lived secret keyed by the new run id (in Azure Storage).
   - Records the run id in the durable run-id index.
   - Schedules the run in Durable Task Scheduler.
3. DTS walks the run through plan → work → review → publish, persisting state at each step.
4. ACA Sandboxes execute each step (one sandbox per task) using the `github-copilot-sdk` package and the run-scoped PAT.
5. Read APIs (`GET /api/swarm-runs/{runId}`, `/plan`, `/tasks`, `/details`, `/events`, `/sandboxes/{sandboxId}/logstream`) resolve state by run id and rebuild responses from DTS.
6. The PAT is never returned in API responses and never written into DTS state.

## Runtime responsibilities

### Durable Task Scheduler

Owns:

- Run scheduling.
- Plan-feedback as an external event.
- Suspend / resume / cancel / purge / rerun controls.
- The run state used to rebuild summary, plan, tasks, details, and event responses.
- History-bounded progression for longer execution chains.

### Azure Storage

Used for two narrow concerns:

- The durable run-id index used by `GET /api/swarm-runs`.
- Short-lived per-run GitHub PAT secrets.

### ACA Sandboxes

The ACA integration lives under `src/agent_swarm_service/sandboxes/`:

- `aca_client.py` — sandbox lifecycle and command execution.
- `sandbox_groups.py` — sandbox group resolution.
- `workspace.py` — staged `/workspace/.swarm` payloads (request, result, log).
- `logs.py` — mirrored log tail and redaction helpers.

These call `azure-sandbox` and `azure-mgmt-sandbox` directly. Sandboxes:

- Execute planner, worker, and reviewer steps.
- Honor the run-scoped Copilot auth contract (`github-copilot-sdk` + `GH_TOKEN`/`GITHUB_TOKEN`; `COPILOT_GITHUB_TOKEN` only when the runtime is explicitly configured to request it).
- Mirror their stdout/stderr to `/workspace/.swarm/logstream.log` so the streaming endpoint can tail it.
- Harvest results from `/workspace/.swarm/result.json`.
- Only expose log streams while the sandbox is still active for the current run.

### Sandbox image contract

Every sandbox runs a private DiskId. The image must include:

- Python 3.11+ (3.12 recommended)
- `git`
- CA certificates for outbound TLS
- `pip`
- `github-copilot-sdk`
- `/bin/sh`
- A writable `/workspace`
- Two baked entry points the service invokes:
  - `/opt/agent-swarm/run-role.py`
  - `/opt/agent-swarm/copilot_runtime.py`

The repo ships a compatible image at `sandbox-image/`. See
[`sandbox-image/README.md`](../sandbox-image/README.md) for
build and publish steps.

DiskId resolution order is:

1. Per-run override (`options.sandboxDiskId` on `POST /api/swarm-runs`).
2. Service deployment default (`SWARM_SANDBOX_DISK_ID`).

If neither layer supplies a DiskId, run creation fails before DTS starts.

## Validation anchors

The automated suite covers:

- Request validation (`POST /api/swarm-runs` requires `githubPat`).
- DTS-backed run-id route behavior across the swarm-runs surface.
- DTS-oriented create / list / get / control / projection behavior.
- Sandbox + logstream behavior through the ACA sandbox modules the service runs in production.
- Docs and infra layout alignment for the `azd up` path.

Run them with:

```powershell
python -m pytest -q
```
