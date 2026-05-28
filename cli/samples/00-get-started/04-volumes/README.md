# 04 - Volumes

Persistent storage that survives sandbox deletion. Two flavors:

- **AzureBlob** — shared across sandboxes; great for producer/consumer
  pipelines or fan-out result aggregation.
- **DataDisk** — block storage; one sandbox at a time, lower latency.

Create a volume on the group, then mount it into one or more sandboxes
with `add_volume_mount` (after create) or by passing `volumes=[...]`
to `begin_create_sandbox`.

- [`python/`](python/) — `group.create_volume(...)` + `sandbox.add_volume_mount(AddVolumeMountRequest(...))`
- [`cli/`](cli/) — `aca sandboxgroup volume create` + `aca sandbox mount`

## What's covered

| API | Python | CLI |
|---|---|---|
| Create AzureBlob volume | `create_volume("name", type="AzureBlob")` | `volume create --name X --type AzureBlob` |
| Create DataDisk volume | `create_volume("name", type="DataDisk", size="1Gi")` | `volume create --name X --type DataDisk --size 1Gi` |
| List | `list_volumes()` | `volume list` |
| Mount on existing sandbox | `sandbox.add_volume_mount(AddVolumeMountRequest(...))` | `aca sandbox mount --volume X --path /mnt/x` |
| Mount at create-time | `begin_create_sandbox(volumes=[SandboxVolume(...)])` | `aca sandbox apply --file ...` |
| Delete | `delete_volume("name")` | `volume delete --name X` |

## Demo flow

1. Create an AzureBlob volume
2. Spin up a **producer** sandbox, mount the volume, write `/mnt/shared/output.json`
3. Spin up a **consumer** sandbox, mount the same volume, `cat` the file
4. Tear down both sandboxes; delete the volume

## Why this matters

Volumes are the standard answer to "how do my sandboxes share state?"
For LLM workloads: cache model weights once, mount everywhere; persist
intermediate tool outputs across an agent's turns; coordinate parallel
workers via blob writes.

## What the platform provides vs. what you write

Volumes exist because building "shared storage between sandboxes"
yourself is a lot of moving parts. Here is exactly what the platform
absorbs when you call `create_volume(type="AzureBlob")` and pass
`volumes=[SandboxVolume(...)]` at create time:

| Concern | Provided by the platform | What you'd write without it |
|---|---|---|
| Storage account, container, lifecycle | ✅ behind `create_volume(...)` | Provision a storage account; create a container; track them per-tenant |
| Identity for sandbox → blob access | ✅ transparent to the sandbox | Issue SAS, or grant MI `Storage Blob Data *` on the right scope, and wait for AAD propagation |
| Secrets, SAS rotation | ✅ none in your code | Manage SAS expiry / rotation, or store connection strings somewhere safe |
| Mount point inside the sandbox | ✅ via `SandboxVolume(volume_name, mountpoint)` | Install + configure `blobfuse2` (or similar) inside every image |
| Network reachability from the sandbox | ✅ private path inside the sandbox network | Open egress / private endpoints / firewall rules to reach the storage account |
| Authorisation between sandboxes in the same group | ✅ implicit (all sandboxes in the group can mount the group's volumes) | Stamp out RBAC per-sandbox-identity per-container |
| Cross-tenant isolation | ✅ one volume scope per sandbox group | Manage container-per-tenant + RBAC per-tenant |
| Your producer/consumer logic | — | `open()` / `os.replace()` / `glob.glob()` |

What you write in the sandbox stays as plain stdlib (`open`,
`glob`, `os.replace`). No `azure-storage-blob`, no
`BlobServiceClient`, no SAS, no `DefaultAzureCredential` against
storage. The volume *is* the API.

The same primitive backs the
[`04-swarms / 02-shared-blob-memory`](../../scenarios/04-swarms/02-shared-blob-memory/)
scenario, where a swarm of agents uses one volume as a durable
shared scratchpad.
