"""Durable Task Scheduler sandbox workflow lab.

Usage:
    python labs/02-durable-task-workflows/main.py
    python labs/02-durable-task-workflows/main.py --assign-current-user-role --stop-and-resume
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import textwrap
import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    import durabletask.task as task
    from azure.identity import DefaultAzureCredential
    from durabletask.azuremanaged.client import DurableTaskSchedulerClient
    from durabletask.azuremanaged.worker import DurableTaskSchedulerWorker

logger = logging.getLogger("dts-sandbox-lab")
DASHBOARD_URL = "https://dashboard.durabletask.io/"
SINGLE_ORCHESTRATOR = "sandbox_lifecycle_orchestrator"
FANOUT_ORCHESTRATOR = "sandbox_fan_out_orchestrator"


@dataclass(frozen=True)
class LabConfig:
    lab_name: str
    subscription_id: str
    resource_group: str
    location: str
    scheduler_name: str
    task_hub: str
    sandbox_group: str
    scheduler_sku: str = "Consumption"
    scheduler_capacity: int = 1
    ip_allowlist: str = "[0.0.0.0/0]"


@dataclass(frozen=True)
class WorkflowRuntime:
    endpoint: str
    taskhub: str
    worker: DurableTaskSchedulerWorker
    client: DurableTaskSchedulerClient


def sanitize_name(value: str, *, max_length: int = 40) -> str:
    slug = re.sub(r"[^a-z0-9-]+", "-", value.lower())
    slug = re.sub(r"-+", "-", slug).strip("-")
    return (slug or "lab")[:max_length].strip("-") or "lab"


def lab_name_from_path(path: Path | None = None) -> str:
    return (path or Path(__file__).resolve().parent).name


def build_default_names(lab_name: str, subscription_id: str) -> dict[str, str]:
    suffix = subscription_id.replace("-", "")[:6].lower()
    base = sanitize_name(lab_name, max_length=24)
    return {
        "resource_group": f"lab-{base}",
        "scheduler_name": sanitize_name(f"{base}-{suffix}-dts", max_length=44),
        "task_hub": sanitize_name(f"{base}-hub", max_length=32),
        "sandbox_group": sanitize_name(f"{base}-sg", max_length=32),
    }


@lru_cache(maxsize=1)
def resolve_az_executable() -> str:
    override = os.environ.get("AZURE_CLI_PATH")
    if override and Path(override).is_file():
        return override

    candidates = ("az.cmd", "az.exe", "az") if os.name == "nt" else ("az", "az.cmd", "az.exe")
    for candidate in candidates:
        resolved = shutil.which(candidate)
        if resolved:
            resolved_path = Path(resolved)
            if os.name == "nt" and not resolved_path.suffix:
                for suffix in (".cmd", ".exe", ".bat"):
                    alternate = resolved_path.with_suffix(suffix)
                    if alternate.is_file():
                        return str(alternate)
            return resolved

    if os.name == "nt":
        known_paths = []
        for env_name in ("ProgramFiles(x86)", "ProgramFiles"):
            base = os.environ.get(env_name)
            if base:
                known_paths.append(Path(base) / "Microsoft SDKs" / "Azure" / "CLI2" / "wbin" / "az.cmd")

        local_app_data = os.environ.get("LocalAppData")
        if local_app_data:
            known_paths.append(Path(local_app_data) / "Programs" / "Azure CLI" / "wbin" / "az.cmd")

        for path in known_paths:
            if path.is_file():
                return str(path)

    raise RuntimeError(
        "Azure CLI executable not found for this Python environment. "
        "Install Azure CLI, or make `az`/`az.cmd` available on PATH before running the lab. "
        "On Windows, restarting VS Code after installing Azure CLI or adding the Azure CLI "
        "`wbin` directory to PATH usually fixes this."
    )


def normalize_command(command: list[str]) -> list[str]:
    if command and command[0] == "az":
        return [resolve_az_executable(), *command[1:]]
    return command


def run_command(command: list[str], *, check: bool = True) -> subprocess.CompletedProcess[str]:
    command = normalize_command(command)
    logger.info("$ %s", subprocess.list2cmdline(command))
    try:
        completed = subprocess.run(command, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise RuntimeError(f"Command not found: {command[0]}") from exc
    if check and completed.returncode != 0:
        message = completed.stderr.strip() or completed.stdout.strip() or "unknown error"
        raise RuntimeError(f"Command failed: {subprocess.list2cmdline(command)}\n{message}")
    return completed


def run_az_json(command: list[str]) -> Any:
    completed = run_command([*command, "--only-show-errors", "-o", "json"])
    text = completed.stdout.strip()
    return json.loads(text) if text else {}


def try_run_az_json(command: list[str]) -> Any | None:
    completed = run_command([*command, "--only-show-errors", "-o", "json"], check=False)
    if completed.returncode != 0:
        return None
    text = completed.stdout.strip()
    return json.loads(text) if text else {}


def run_az_tsv(command: list[str]) -> str:
    completed = run_command([*command, "--only-show-errors", "-o", "tsv"])
    return completed.stdout.strip()


def load_account() -> dict[str, Any]:
    return run_az_json(["az", "account", "show"])


def add_vendored_sandbox_wheels_to_path() -> None:
    current_dir = Path(__file__).resolve().parent
    for base_dir in (current_dir, *current_dir.parents):
        wheel_dir = base_dir / "vendor" / "wheels"
        if not wheel_dir.is_dir():
            continue
        for pattern in ("azure_sandbox-*.whl", "azure_mgmt_sandbox-*.whl"):
            for wheel_path in sorted(wheel_dir.glob(pattern)):
                wheel_str = str(wheel_path)
                if wheel_str not in sys.path:
                    sys.path.insert(0, wheel_str)
        return


def import_sandbox_clients() -> tuple[Any, Any]:
    try:
        from azure.mgmt.sandbox import SandboxGroupManagementClient
        from azure.sandbox import SandboxClient

        return SandboxClient, SandboxGroupManagementClient
    except ImportError:
        add_vendored_sandbox_wheels_to_path()
        try:
            from azure.mgmt.sandbox import SandboxGroupManagementClient
            from azure.sandbox import SandboxClient

            return SandboxClient, SandboxGroupManagementClient
        except ImportError as exc:
            raise ImportError(
                "Unable to import `azure.sandbox` and `azure.mgmt.sandbox`. "
                "Install the sandbox wheels from the GitHub Release for "
                "Azure-Samples/azure-container-apps-sandboxes, or place vendored wheels in "
                "`vendor\\wheels` so the lab can load them as a local fallback."
            ) from exc


@dataclass
class SandboxWorkflowClient:
    sandbox: Any
    sandbox_groups: Any

    @classmethod
    def create(cls, *, subscription_id: str, resource_group: str) -> "SandboxWorkflowClient":
        SandboxClient, SandboxGroupManagementClient = import_sandbox_clients()
        credential = create_runtime_credential()
        try:
            sandbox = SandboxClient(
                subscription_id=subscription_id,
                resource_group=resource_group,
                credential=credential,
            )
        except TypeError:
            sandbox = SandboxClient(subscription_id=subscription_id, resource_group=resource_group)
        try:
            sandbox_groups = SandboxGroupManagementClient(
                subscription_id=subscription_id,
                resource_group=resource_group,
                credential=credential,
            )
        except TypeError:
            sandbox_groups = SandboxGroupManagementClient(
                subscription_id=subscription_id,
                resource_group=resource_group,
            )
        return cls(sandbox=sandbox, sandbox_groups=sandbox_groups)

    def create_group(self, name: str, *, location: str) -> dict[str, Any]:
        return self.sandbox_groups.create_group(name, location=location)

    def delete_group(self, name: str) -> None:
        self.sandbox_groups.delete_group(name)

    def __getattr__(self, attr: str) -> Any:
        return getattr(self.sandbox, attr)


def import_durabletask_dependencies() -> tuple[Any, Any, Any, Any, Any]:
    import durabletask.task as task
    from azure.identity import AzureCliCredential, DefaultAzureCredential
    from durabletask.azuremanaged.client import DurableTaskSchedulerClient
    from durabletask.azuremanaged.worker import DurableTaskSchedulerWorker

    return task, AzureCliCredential, DefaultAzureCredential, DurableTaskSchedulerClient, DurableTaskSchedulerWorker


def has_explicit_azure_identity_configuration() -> bool:
    identity_settings = (
        "AZURE_CLIENT_ID",
        "AZURE_TENANT_ID",
        "AZURE_CLIENT_SECRET",
        "AZURE_CLIENT_CERTIFICATE_PATH",
        "AZURE_FEDERATED_TOKEN_FILE",
        "IDENTITY_ENDPOINT",
        "IDENTITY_HEADER",
        "IMDS_ENDPOINT",
        "MSI_ENDPOINT",
        "MSI_SECRET",
    )
    return any(os.environ.get(name) for name in identity_settings)


def create_runtime_credential() -> Any:
    _, AzureCliCredential, DefaultAzureCredential, _, _ = import_durabletask_dependencies()
    if not has_explicit_azure_identity_configuration():
        try:
            resolve_az_executable()
        except RuntimeError:
            pass
        else:
            return AzureCliCredential()
    return DefaultAzureCredential()


def ensure_durabletask_extension() -> None:
    try:
        run_command(["az", "extension", "add", "--name", "durabletask", "--upgrade", "--only-show-errors"])
    except RuntimeError:
        run_command(["az", "extension", "add", "--name", "durabletask", "--only-show-errors"])
        run_command(["az", "extension", "update", "--name", "durabletask", "--only-show-errors"], check=False)


def maybe_assign_current_user_role(scheduler_id: str, principal_name: str) -> dict[str, Any]:
    summary = {"principal": principal_name, "role": "Durable Task Data Contributor"}
    try:
        existing = run_az_json([
            "az",
            "role",
            "assignment",
            "list",
            "--assignee",
            principal_name,
            "--scope",
            scheduler_id,
            "--role",
            "Durable Task Data Contributor",
        ])
        if existing:
            summary["status"] = "already-assigned"
            return summary

        run_az_json([
            "az",
            "role",
            "assignment",
            "create",
            "--assignee",
            principal_name,
            "--role",
            "Durable Task Data Contributor",
            "--scope",
            scheduler_id,
        ])
        summary["status"] = "created"
        return summary
    except RuntimeError as exc:
        summary["status"] = "failed"
        summary["error"] = str(exc)
        return summary


def provision_dts_resources(
    config: LabConfig,
    *,
    assign_current_user_role: bool = False,
    principal_name: str | None = None,
) -> dict[str, Any]:
    ensure_durabletask_extension()
    run_command([
        "az",
        "group",
        "create",
        "--name",
        config.resource_group,
        "--location",
        config.location,
        "--only-show-errors",
        "-o",
        "none",
    ])

    scheduler = try_run_az_json([
        "az",
        "durabletask",
        "scheduler",
        "show",
        "--resource-group",
        config.resource_group,
        "--name",
        config.scheduler_name,
    ])
    if scheduler is None:
        create_command = [
            "az",
            "durabletask",
            "scheduler",
            "create",
            "--resource-group",
            config.resource_group,
            "--name",
            config.scheduler_name,
            "--location",
            config.location,
            "--ip-allowlist",
            config.ip_allowlist,
            "--sku-name",
            config.scheduler_sku.title(),
        ]
        if config.scheduler_sku.lower() == "dedicated":
            create_command.extend(["--sku-capacity", str(config.scheduler_capacity)])
        run_az_json(create_command)
        run_command([
            "az",
            "durabletask",
            "scheduler",
            "wait",
            "--resource-group",
            config.resource_group,
            "--name",
            config.scheduler_name,
            "--created",
            "--interval",
            "15",
            "--timeout",
            "1800",
            "--only-show-errors",
        ])
        scheduler = run_az_json([
            "az",
            "durabletask",
            "scheduler",
            "show",
            "--resource-group",
            config.resource_group,
            "--name",
            config.scheduler_name,
        ])

    taskhub = try_run_az_json([
        "az",
        "durabletask",
        "taskhub",
        "show",
        "--resource-group",
        config.resource_group,
        "--scheduler-name",
        config.scheduler_name,
        "--name",
        config.task_hub,
    ])
    if taskhub is None:
        run_az_json([
            "az",
            "durabletask",
            "taskhub",
            "create",
            "--resource-group",
            config.resource_group,
            "--scheduler-name",
            config.scheduler_name,
            "--name",
            config.task_hub,
        ])
        run_command([
            "az",
            "durabletask",
            "taskhub",
            "wait",
            "--resource-group",
            config.resource_group,
            "--scheduler-name",
            config.scheduler_name,
            "--name",
            config.task_hub,
            "--created",
            "--interval",
            "15",
            "--timeout",
            "600",
            "--only-show-errors",
        ])
        taskhub = run_az_json([
            "az",
            "durabletask",
            "taskhub",
            "show",
            "--resource-group",
            config.resource_group,
            "--scheduler-name",
            config.scheduler_name,
            "--name",
            config.task_hub,
        ])

    endpoint = scheduler["properties"]["endpoint"]
    os.environ["DTS_ENDPOINT"] = endpoint
    os.environ["DTS_TASKHUB"] = config.task_hub

    summary = {
        "resource_group": config.resource_group,
        "scheduler_name": config.scheduler_name,
        "scheduler_id": scheduler["id"],
        "endpoint": endpoint,
        "task_hub": config.task_hub,
        "taskhub_id": taskhub["id"],
        "dashboard_url": DASHBOARD_URL,
    }
    if assign_current_user_role and principal_name:
        summary["role_assignment"] = maybe_assign_current_user_role(scheduler["id"], principal_name)
    return summary


def parse_json_payload(text: str | None) -> Any:
    if not text:
        return None
    text = text.strip()
    if not text:
        return None
    try:
        return json.loads(text)
    except json.JSONDecodeError:
        return text


def parse_last_json_line(stdout: str | None) -> Any:
    if not stdout:
        return None
    for line in reversed(stdout.splitlines()):
        candidate = line.strip()
        if not candidate:
            continue
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            continue
    return stdout.strip()


def compact_stats(stats: dict[str, Any]) -> dict[str, Any]:
    memory = stats.get("memory") or {}
    cpu = stats.get("cpu") or {}
    return {
        "memory_used_mb": round(int(memory.get("usedBytes", 0)) / 1024 / 1024, 2),
        "memory_total_mb": round(int(memory.get("totalBytes", 0)) / 1024 / 1024, 2),
        "cpu_nano_cores": cpu.get("usageNanoCores"),
    }


def render_workload(job_name: str, numbers: list[int], sleep_seconds: int) -> str:
    count = len(numbers)
    total = sum(numbers)
    max_value = "null" if not numbers else str(max(numbers))
    return textwrap.dedent(
        f"""\
        #!/bin/sh
        sleep {int(sleep_seconds)}
        hostname_value="$(hostname 2>/dev/null || uname -n || printf unknown)"
        printf '{{"job_name":"%s","count":{count},"sum":{total},"max":{max_value},"hostname":"%s","python":null}}\n' {shlex.quote(job_name)} "$hostname_value"
        """
    ).strip()


def sandbox_shell_command(script_path: str) -> str:
    return f"sh {shlex.quote(script_path)}"


def create_runtime(config: LabConfig, *, endpoint: str | None = None, taskhub: str | None = None) -> WorkflowRuntime:
    _, _, _, DurableTaskSchedulerClient, DurableTaskSchedulerWorker = import_durabletask_dependencies()
    resolved_endpoint = endpoint or os.environ.get("DTS_ENDPOINT")
    resolved_taskhub = taskhub or os.environ.get("DTS_TASKHUB") or config.task_hub
    if not resolved_endpoint:
        raise ValueError("A DTS endpoint is required. Provision DTS first or set DTS_ENDPOINT.")

    credential = create_runtime_credential()
    secure_channel = resolved_endpoint.lower().startswith("https://")
    worker = DurableTaskSchedulerWorker(
        host_address=resolved_endpoint,
        taskhub=resolved_taskhub,
        token_credential=credential,
        secure_channel=secure_channel,
    )
    register_workflows(worker, config)
    client = DurableTaskSchedulerClient(
        host_address=resolved_endpoint,
        taskhub=resolved_taskhub,
        token_credential=credential,
        secure_channel=secure_channel,
    )
    return WorkflowRuntime(
        endpoint=resolved_endpoint,
        taskhub=resolved_taskhub,
        worker=worker,
        client=client,
    )


def start_runtime(runtime: WorkflowRuntime) -> None:
    runtime.worker.start()
    logger.info("Durable Task worker started in this Python process.")


def stop_runtime(runtime: WorkflowRuntime | None) -> None:
    if runtime is None:
        return
    runtime.worker.stop()
    logger.info("Durable Task worker stopped.")


def register_workflows(worker: Any, config: LabConfig) -> None:
    task, _, _, _, _ = import_durabletask_dependencies()
    sandbox_client = SandboxWorkflowClient.create(
        subscription_id=config.subscription_id,
        resource_group=config.resource_group,
    )

    def ensure_sandbox_group_activity(_: Any, payload: dict[str, Any]) -> dict[str, Any]:
        sandbox_group = payload.get("sandbox_group", config.sandbox_group)
        location = payload.get("location", config.location)
        group = sandbox_client.create_group(sandbox_group, location=location)
        return {
            "sandbox_group": group["name"],
            "location": group.get("location", location),
        }

    def create_sandbox_activity(_: Any, payload: dict[str, Any]) -> dict[str, Any]:
        sandbox_group = payload.get("sandbox_group", config.sandbox_group)
        sandbox = sandbox_client.create_sandbox(sandbox_group, disk=payload.get("disk", "ubuntu"))
        return {
            "sandbox_id": sandbox["id"],
            "sandbox_state": sandbox.get("state"),
        }

    def stage_workload_activity(_: Any, payload: dict[str, Any]) -> dict[str, Any]:
        sandbox_id = payload["sandbox_id"]
        sandbox_group = payload.get("sandbox_group", config.sandbox_group)
        job_name = payload.get("job_name", "primary-job")
        workload_name = sanitize_name(job_name, max_length=24)
        workload_path = f"/workspace/{workload_name}.sh"
        sandbox_client.exec(sandbox_id, sandbox_group, "mkdir -p /workspace")
        sandbox_client.write_file(
            sandbox_id,
            sandbox_group,
            workload_path,
            render_workload(
                job_name=job_name,
                numbers=[int(value) for value in payload.get("numbers", [2, 4, 8])],
                sleep_seconds=int(payload.get("sleep_seconds", 1)),
            ),
        )
        return {"workload_path": workload_path}

    def execute_workload_activity(_: Any, payload: dict[str, Any]) -> dict[str, Any]:
        sandbox_id = payload["sandbox_id"]
        sandbox_group = payload.get("sandbox_group", config.sandbox_group)
        result = sandbox_client.exec(sandbox_id, sandbox_group, sandbox_shell_command(payload["workload_path"]))
        exit_code = result.get("exitCode", 1)
        stderr = (result.get("stderr") or "").strip()
        if exit_code != 0:
            raise RuntimeError(stderr or result.get("stdout") or "sandbox workload failed")
        return {
            "exit_code": exit_code,
            "workload_output": parse_last_json_line(result.get("stdout")),
            "stderr": stderr or None,
        }

    def capture_stats_activity(_: Any, payload: dict[str, Any]) -> dict[str, Any]:
        sandbox_id = payload["sandbox_id"]
        sandbox_group = payload.get("sandbox_group", config.sandbox_group)
        stats = sandbox_client.get_stats(sandbox_id, sandbox_group)
        return {"stats": compact_stats(stats)}

    def create_snapshot_activity(_: Any, payload: dict[str, Any]) -> dict[str, Any]:
        sandbox_id = payload["sandbox_id"]
        sandbox_group = payload.get("sandbox_group", config.sandbox_group)
        snapshot_name = sanitize_name(f"{payload.get('job_name', 'job')}-snapshot", max_length=28)
        snapshot = sandbox_client.create_snapshot(sandbox_id, sandbox_group, name=snapshot_name)
        return {
            "snapshot_id": snapshot.get("id"),
            "snapshot_name": snapshot_name,
        }

    def stop_resume_sandbox_activity(_: Any, payload: dict[str, Any]) -> dict[str, Any]:
        sandbox_id = payload["sandbox_id"]
        sandbox_group = payload.get("sandbox_group", config.sandbox_group)
        sandbox_client.stop_sandbox(sandbox_id, sandbox_group)
        time.sleep(2)
        sandbox_client.resume_sandbox(sandbox_id, sandbox_group)
        time.sleep(5)
        resumed = sandbox_client.exec(sandbox_id, sandbox_group, "printf 'resume-ok\n'")
        return {
            "resume_check": {
                "exit_code": resumed.get("exitCode", 1),
                "stdout": (resumed.get("stdout") or "").strip(),
            }
        }

    def cleanup_sandbox_activity(_: Any, payload: dict[str, Any]) -> dict[str, Any]:
        sandbox_client.delete_sandbox(payload["sandbox_id"], payload.get("sandbox_group", config.sandbox_group))
        return {
            "cleanup": {
                "requested": True,
                "sandbox_deleted": True,
            }
        }

    def run_sandbox_job_activity(_: Any, payload: dict[str, Any]) -> dict[str, Any]:
        sandbox_group = payload.get("sandbox_group", config.sandbox_group)
        sandbox = sandbox_client.create_sandbox(sandbox_group, disk=payload.get("disk", "ubuntu"))
        sandbox_id = sandbox["id"]
        cleanup_requested = bool(payload.get("cleanup", False))
        cleanup_summary = {
            "requested": cleanup_requested,
            "sandbox_deleted": False,
        }
        result_payload: dict[str, Any] = {
            "job_name": payload.get("job_name", "fan-out-job"),
            "sandbox_id": sandbox_id,
            "cleanup": cleanup_summary,
        }

        try:
            workload_name = sanitize_name(result_payload["job_name"], max_length=24)
            workload_path = f"/workspace/{workload_name}.sh"
            sandbox_client.exec(sandbox_id, sandbox_group, "mkdir -p /workspace")
            sandbox_client.write_file(
                sandbox_id,
                sandbox_group,
                workload_path,
                render_workload(
                    job_name=result_payload["job_name"],
                    numbers=[int(value) for value in payload.get("numbers", [1, 2, 3])],
                    sleep_seconds=int(payload.get("sleep_seconds", 1)),
                ),
            )
            execution = sandbox_client.exec(sandbox_id, sandbox_group, sandbox_shell_command(workload_path))
            exit_code = execution.get("exitCode", 1)
            stderr = (execution.get("stderr") or "").strip()
            if exit_code != 0:
                raise RuntimeError(stderr or execution.get("stdout") or "sandbox workload failed")

            snapshot_name = sanitize_name(f"{result_payload['job_name']}-snapshot", max_length=28)
            snapshot = sandbox_client.create_snapshot(sandbox_id, sandbox_group, name=snapshot_name)

            resume_check = None
            if payload.get("stop_and_resume", False):
                sandbox_client.stop_sandbox(sandbox_id, sandbox_group)
                time.sleep(2)
                sandbox_client.resume_sandbox(sandbox_id, sandbox_group)
                time.sleep(5)
                resumed = sandbox_client.exec(sandbox_id, sandbox_group, "printf 'resume-ok\n'")
                resume_check = {
                    "exit_code": resumed.get("exitCode", 1),
                    "stdout": (resumed.get("stdout") or "").strip(),
                }

            result_payload.update(
                {
                    "exit_code": exit_code,
                    "workload_output": parse_last_json_line(execution.get("stdout")),
                    "stderr": stderr or None,
                    "stats": compact_stats(sandbox_client.get_stats(sandbox_id, sandbox_group)),
                    "snapshot_id": snapshot.get("id"),
                    "snapshot_name": snapshot_name,
                    "resume_check": resume_check,
                }
            )
            return result_payload
        finally:
            if cleanup_requested:
                sandbox_client.delete_sandbox(sandbox_id, sandbox_group)
                cleanup_summary["sandbox_deleted"] = True

    def sandbox_lifecycle_orchestrator(ctx: Any, payload: dict[str, Any]) -> Any:
        working = dict(payload or {})

        ctx.set_custom_status({"phase": "ensure-sandbox-group"})
        working.update((yield ctx.call_activity("ensure_sandbox_group_activity", input=working)) or {})

        ctx.set_custom_status({"phase": "create-sandbox"})
        working.update((yield ctx.call_activity("create_sandbox_activity", input=working)) or {})

        ctx.set_custom_status({"phase": "stage-workload"})
        working.update((yield ctx.call_activity("stage_workload_activity", input=working)) or {})

        ctx.set_custom_status({"phase": "execute-workload"})
        working.update((yield ctx.call_activity("execute_workload_activity", input=working)) or {})

        ctx.set_custom_status({"phase": "capture-stats"})
        working.update((yield ctx.call_activity("capture_stats_activity", input=working)) or {})

        ctx.set_custom_status({"phase": "snapshot"})
        working.update((yield ctx.call_activity("create_snapshot_activity", input=working)) or {})

        if working.get("stop_and_resume"):
            ctx.set_custom_status({"phase": "stop-resume"})
            working.update((yield ctx.call_activity("stop_resume_sandbox_activity", input=working)) or {})

        if working.get("cleanup"):
            ctx.set_custom_status({"phase": "cleanup"})
            working.update((yield ctx.call_activity("cleanup_sandbox_activity", input=working)) or {})
        else:
            working["cleanup"] = {
                "requested": False,
                "sandbox_deleted": False,
            }

        return {
            "job_name": working.get("job_name"),
            "sandbox_group": working.get("sandbox_group", config.sandbox_group),
            "sandbox_id": working.get("sandbox_id"),
            "workload_path": working.get("workload_path"),
            "exit_code": working.get("exit_code"),
            "workload_output": working.get("workload_output"),
            "stats": working.get("stats"),
            "snapshot_id": working.get("snapshot_id"),
            "snapshot_name": working.get("snapshot_name"),
            "resume_check": working.get("resume_check"),
            "cleanup": working.get("cleanup"),
        }

    def sandbox_fan_out_orchestrator(ctx: Any, payload: dict[str, Any]) -> Any:
        base_payload = dict(payload or {})
        yield ctx.call_activity("ensure_sandbox_group_activity", input=base_payload)

        jobs = []
        for item in base_payload.get("jobs", []):
            jobs.append(
                ctx.call_activity(
                    "run_sandbox_job_activity",
                    input={
                        "sandbox_group": config.sandbox_group,
                        "location": config.location,
                        "cleanup": base_payload.get("cleanup", False),
                        "stop_and_resume": base_payload.get("stop_and_resume", False),
                        **item,
                    },
                )
            )

        results = yield task.when_all(jobs)
        aggregate_sum = 0
        compact_results = []
        for item in results:
            workload_output = item.get("workload_output") or {}
            if isinstance(workload_output, dict):
                aggregate_sum += int(workload_output.get("sum", 0) or 0)
            compact_results.append(
                {
                    "job_name": item.get("job_name"),
                    "sandbox_id": item.get("sandbox_id"),
                    "exit_code": item.get("exit_code"),
                    "workload_output": workload_output,
                    "snapshot_id": item.get("snapshot_id"),
                    "cleanup": item.get("cleanup"),
                }
            )

        return {
            "job_count": len(compact_results),
            "aggregate_sum": aggregate_sum,
            "jobs": compact_results,
        }

    worker.add_activity(ensure_sandbox_group_activity)
    worker.add_activity(create_sandbox_activity)
    worker.add_activity(stage_workload_activity)
    worker.add_activity(execute_workload_activity)
    worker.add_activity(capture_stats_activity)
    worker.add_activity(create_snapshot_activity)
    worker.add_activity(stop_resume_sandbox_activity)
    worker.add_activity(cleanup_sandbox_activity)
    worker.add_activity(run_sandbox_job_activity)
    worker.add_orchestrator(sandbox_lifecycle_orchestrator)
    worker.add_orchestrator(sandbox_fan_out_orchestrator)


def orchestration_summary(state: Any) -> dict[str, Any]:
    if state is None:
        raise RuntimeError("No orchestration state was returned.")
    state.raise_if_failed()
    return {
        "instance_id": state.instance_id,
        "runtime_status": getattr(state.runtime_status, "name", str(state.runtime_status)),
        "output": parse_json_payload(state.serialized_output),
    }


def schedule_and_wait(
    client: DurableTaskSchedulerClient,
    orchestrator_name: str,
    *,
    payload: dict[str, Any],
    timeout: int = 900,
    tags: dict[str, str] | None = None,
) -> dict[str, Any]:
    instance_id = client.schedule_new_orchestration(orchestrator_name, input=payload, tags=tags)
    state = client.wait_for_orchestration_completion(instance_id, timeout=timeout)
    summary = orchestration_summary(state)
    summary["instance_id"] = instance_id
    return summary


def run_single_workflow(
    client: DurableTaskSchedulerClient,
    config: LabConfig,
    *,
    cleanup_sandboxes: bool = False,
    stop_and_resume: bool = False,
    timeout: int = 900,
) -> dict[str, Any]:
    payload = {
        "job_name": "primary-sandbox-job",
        "sandbox_group": config.sandbox_group,
        "numbers": [3, 5, 8, 13],
        "sleep_seconds": 1,
        "cleanup": cleanup_sandboxes,
        "stop_and_resume": stop_and_resume,
    }
    return schedule_and_wait(
        client,
        SINGLE_ORCHESTRATOR,
        payload=payload,
        timeout=timeout,
        tags={"lab": config.lab_name, "scenario": "single"},
    )


def run_fan_out_workflow(
    client: DurableTaskSchedulerClient,
    config: LabConfig,
    *,
    fan_out_width: int = 2,
    cleanup_sandboxes: bool = False,
    timeout: int = 900,
) -> dict[str, Any]:
    jobs = []
    for index in range(1, max(fan_out_width, 1) + 1):
        jobs.append(
            {
                "job_name": f"fanout-job-{index}",
                "numbers": [index, index + 1, index + 2],
                "sleep_seconds": 1,
            }
        )

    return schedule_and_wait(
        client,
        FANOUT_ORCHESTRATOR,
        payload={
            "cleanup": cleanup_sandboxes,
            "jobs": jobs,
        },
        timeout=timeout,
        tags={"lab": config.lab_name, "scenario": "fanout"},
    )


def extract_sandbox_ids(result: dict[str, Any] | None) -> list[str]:
    if not result:
        return []
    payload = result.get("output") or {}
    sandbox_ids: list[str] = []
    sandbox_id = payload.get("sandbox_id")
    if sandbox_id:
        sandbox_ids.append(sandbox_id)
    for item in payload.get("jobs", []):
        job_sandbox_id = item.get("sandbox_id")
        if job_sandbox_id:
            sandbox_ids.append(job_sandbox_id)
    return sorted(set(sandbox_ids))


def cleanup_sandboxes_by_id(config: LabConfig, sandbox_ids: list[str]) -> list[dict[str, Any]]:
    client = SandboxWorkflowClient.create(
        subscription_id=config.subscription_id,
        resource_group=config.resource_group,
    )
    results: list[dict[str, Any]] = []
    for sandbox_id in sorted(set(sandbox_ids)):
        try:
            client.delete_sandbox(sandbox_id, config.sandbox_group)
            results.append({"sandbox_id": sandbox_id, "deleted": True})
        except Exception as exc:  # noqa: BLE001
            results.append({"sandbox_id": sandbox_id, "deleted": False, "error": str(exc)})
    return results


def delete_sandbox_group_resources(config: LabConfig) -> dict[str, Any]:
    client = SandboxWorkflowClient.create(
        subscription_id=config.subscription_id,
        resource_group=config.resource_group,
    )
    try:
        client.delete_group(config.sandbox_group)
        return {"sandbox_group": config.sandbox_group, "deleted": True}
    except Exception as exc:  # noqa: BLE001
        return {"sandbox_group": config.sandbox_group, "deleted": False, "error": str(exc)}


def delete_dts_resources(config: LabConfig) -> dict[str, Any]:
    taskhub_delete = run_command([
        "az",
        "durabletask",
        "taskhub",
        "delete",
        "--resource-group",
        config.resource_group,
        "--scheduler-name",
        config.scheduler_name,
        "--name",
        config.task_hub,
        "--yes",
        "--only-show-errors",
    ], check=False)
    scheduler_delete = run_command([
        "az",
        "durabletask",
        "scheduler",
        "delete",
        "--resource-group",
        config.resource_group,
        "--name",
        config.scheduler_name,
        "--yes",
        "--only-show-errors",
    ], check=False)
    return {
        "task_hub": {
            "name": config.task_hub,
            "deleted": taskhub_delete.returncode == 0,
        },
        "scheduler": {
            "name": config.scheduler_name,
            "deleted": scheduler_delete.returncode == 0,
        },
    }


def delete_resource_group_resources(config: LabConfig) -> dict[str, Any]:
    run_command([
        "az",
        "group",
        "delete",
        "--name",
        config.resource_group,
        "--yes",
        "--no-wait",
        "--only-show-errors",
    ])
    return {
        "resource_group": config.resource_group,
        "delete_started": True,
    }


def print_runtime_banner(account: dict[str, Any], config: LabConfig, provisioned: dict[str, Any]) -> None:
    print(f"User:             {account['user']['name']}")
    print(f"Subscription:     {account['name']} ({config.subscription_id})")
    print(f"Resource Group:   {config.resource_group}")
    print(f"Location:         {config.location}")
    print(f"Scheduler:        {config.scheduler_name}")
    print(f"Task Hub:         {config.task_hub}")
    print(f"Sandbox Group:    {config.sandbox_group}")
    print(f"DTS Endpoint:     {provisioned['endpoint']}")
    print(f"Dashboard:        {DASHBOARD_URL}")
    print("Worker location:  this Python process (outside every sandbox)")
    print()


def build_config_from_args(args: argparse.Namespace, account: dict[str, Any]) -> LabConfig:
    lab_name = lab_name_from_path()
    defaults = build_default_names(lab_name, account["id"])
    return LabConfig(
        lab_name=lab_name,
        subscription_id=account["id"],
        resource_group=args.resource_group or defaults["resource_group"],
        location=args.location,
        scheduler_name=args.scheduler_name or defaults["scheduler_name"],
        task_hub=args.task_hub or defaults["task_hub"],
        sandbox_group=args.sandbox_group or defaults["sandbox_group"],
        scheduler_sku=args.scheduler_sku,
        scheduler_capacity=args.scheduler_capacity,
        ip_allowlist=args.ip_allowlist,
    )


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Durable Task Scheduler sandbox workflow lab")
    parser.add_argument("-g", "--resource-group", default=None, help="Resource group for DTS and sandbox resources")
    parser.add_argument("-l", "--location", default="westus2", help="Azure region")
    parser.add_argument("--scheduler-name", default=None, help="Durable Task Scheduler name")
    parser.add_argument("--task-hub", default=None, help="Durable Task task hub name")
    parser.add_argument("-s", "--sandbox-group", default=None, help="Sandbox group name")
    parser.add_argument("--scheduler-sku", choices=["Consumption", "Dedicated"], default="Consumption")
    parser.add_argument("--scheduler-capacity", type=int, default=1, help="Dedicated SKU capacity")
    parser.add_argument("--ip-allowlist", default="[0.0.0.0/0]", help="Scheduler IP allowlist JSON string")
    parser.add_argument(
        "--assign-current-user-role",
        action="store_true",
        help="Grant the signed-in user Durable Task Data Contributor on the scheduler",
    )
    parser.add_argument(
        "--cleanup-sandboxes",
        action="store_true",
        help="Delete each sandbox from inside the workflow",
    )
    parser.add_argument(
        "--stop-and-resume",
        action="store_true",
        help="Include a suspend/resume verification step in the primary workflow",
    )
    parser.add_argument("--skip-fan-out", action="store_true", help="Run only the primary workflow")
    parser.add_argument("--fan-out-width", type=int, default=2, help="Parallel sandbox jobs in the fan-out sample")
    parser.add_argument(
        "--delete-sandbox-group",
        action="store_true",
        help="Delete the sandbox group after the workflows finish",
    )
    parser.add_argument(
        "--delete-dts",
        action="store_true",
        help="Delete the DTS task hub and scheduler after the workflows finish",
    )
    parser.add_argument(
        "--delete-resource-group",
        action="store_true",
        help="Delete the entire resource group after the workflows finish",
    )
    parser.add_argument(
        "--provision-only",
        action="store_true",
        help="Provision DTS resources and stop before starting the worker",
    )
    return parser.parse_args()


def main() -> None:
    args = parse_args()
    account = load_account()
    config = build_config_from_args(args, account)
    provisioned = provision_dts_resources(
        config,
        assign_current_user_role=args.assign_current_user_role,
        principal_name=account["user"]["name"],
    )
    print_runtime_banner(account, config, provisioned)

    if args.provision_only:
        return

    runtime: WorkflowRuntime | None = None
    single_result: dict[str, Any] | None = None
    fan_out_result: dict[str, Any] | None = None
    try:
        runtime = create_runtime(config, endpoint=provisioned["endpoint"])
        start_runtime(runtime)

        single_result = run_single_workflow(
            runtime.client,
            config,
            cleanup_sandboxes=args.cleanup_sandboxes,
            stop_and_resume=args.stop_and_resume,
        )
        print("Primary workflow result:")
        print(json.dumps(single_result, indent=2))
        print()

        if not args.skip_fan_out:
            fan_out_result = run_fan_out_workflow(
                runtime.client,
                config,
                fan_out_width=args.fan_out_width,
                cleanup_sandboxes=args.cleanup_sandboxes,
            )
            print("Fan-out workflow result:")
            print(json.dumps(fan_out_result, indent=2))
            print()

        print("Dashboard connection values:")
        print(f"  URL:      {DASHBOARD_URL}")
        print(f"  Endpoint: {provisioned['endpoint']}")
        print(f"  Task hub: {config.task_hub}")
    finally:
        stop_runtime(runtime)

    cleanup_summary: dict[str, Any] = {}
    if args.delete_resource_group:
        cleanup_summary["resource_group"] = delete_resource_group_resources(config)
    else:
        if args.delete_sandbox_group:
            if not args.cleanup_sandboxes:
                sandboxes = extract_sandbox_ids(single_result) + extract_sandbox_ids(fan_out_result)
                if sandboxes:
                    cleanup_summary["sandboxes"] = cleanup_sandboxes_by_id(config, sandboxes)
            cleanup_summary["sandbox_group"] = delete_sandbox_group_resources(config)
        if args.delete_dts:
            cleanup_summary["dts"] = delete_dts_resources(config)

    if cleanup_summary:
        print()
        print("Cleanup summary:")
        print(json.dumps(cleanup_summary, indent=2))


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    main()
