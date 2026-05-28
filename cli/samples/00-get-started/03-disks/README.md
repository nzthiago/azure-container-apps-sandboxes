# 03 - Disks

Every sandbox boots from a **disk image**. Out of the box you can pick a
public image (`ubuntu`, `python-3.12`, `dotnet-10`, …), but the real
power comes from creating your own:

- **Build from a container image** — `disk create --image alpine:3.19`
  → boot future sandboxes from Alpine.
- **Commit a running sandbox** — install packages / download models /
  run setup once, then `sandbox commit` freezes that state into a new
  disk image. Boot the fleet from it; everyone gets the warm-up for free.

This guide does both, end-to-end.

- [`python/`](python/) — `client.begin_create_disk_image(...)` and `sandbox.begin_commit(...)`
- [`cli/`](cli/) — `aca sandboxgroup disk create ...` and `aca sandbox commit ...`

## What's covered

| API                          | Python                                                              | CLI                                                        |
| ---                          | ---                                                                 | ---                                                        |
| **Discover public images**   | `list_public_disk_images()`                                         | `aca sandboxgroup disk list-public`                        |
| **Build from container img** | `begin_create_disk_image("alpine:3.19", name="x")`                  | `aca sandboxgroup disk create --image alpine:3.19 --name x` |
| Private (ACR)                | `begin_create_disk_image(..., registry_credentials=Registry(...))`  | `disk create --image acr.../img --username U --token T`    |
| Managed identity             | `begin_create_disk_image(..., managed_identity_resource_id="...")`  | `disk create --identity system` (or full resource id)      |
| **Commit running sandbox**   | `sandbox.begin_commit(name="x")`                                    | `aca sandbox commit --id $SID --name x`                    |
| **List / get / delete**      | `list_disk_images()` / `get_disk_image(id)` / `delete_disk_image(id)` | `disk list` / `disk get --id` / `disk delete --id`         |
| **Boot from custom disk**    | `begin_create_sandbox(disk_id=image_id)`                            | `aca sandbox create --disk-id $DID`                        |

> `--disk <name>` on `aca sandbox create` only resolves **public** disk
> images (see `disk list-public`). For your own disks, use `--disk-id`.

## Demo flow

The script runs both flows back-to-back:

**Flow A — build from base image**
1. `list_public_disk_images()` — show what's available off the shelf.
2. `begin_create_disk_image("docker.io/library/alpine:3.19", name=…)`
   (5-10 min the first time).
3. `list_disk_images()` + `get_disk_image(id)` — create/list/get convention.
4. Boot a sandbox from the new disk; `cat /etc/alpine-release` proves it
   booted into Alpine.
5. Delete sandbox + disk.

**Flow B — commit a primed sandbox**
1. Boot a primer (default disk).
2. "Prime" it: write `/opt/marker.txt` (stand-in for `pip install ...`,
   model downloads, etc.).
3. `sandbox.begin_commit(name=…)` (5-10 min) freezes the state into a disk.
4. Delete the primer, boot a **new** sandbox from that disk.
5. `read_file("/opt/marker.txt")` — the primed state survived.
6. Delete clone + disk.

> **Heads up**: both flows do real disk builds, so the full guide takes
> **10-20 minutes** end-to-end. The pollers default to 15-20 min timeouts.

## Why this matters

For LLM-agent workloads you want a custom base: pre-baked Python venv +
model weights + your tool binaries. Two patterns map directly:

- **Build-from-image**: `Dockerfile`-style — you already publish your
  agent runtime as an OCI image; build a disk from it once.
- **Commit-from-sandbox**: "golden image as a service" — boot a fresh
  sandbox, install/configure interactively, commit. Useful when there's
  no clean Dockerfile (heavy data downloads, license-gated installers).

Either way: build once, then every subsequent sandbox boots in seconds.
