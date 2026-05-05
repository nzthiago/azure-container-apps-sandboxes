from __future__ import annotations

import json
import unittest
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

from contract_support import ensure_src_on_path

ensure_src_on_path()

from azure.containerapps.sandbox import ExecResult
from fastapi import HTTPException

from agent_swarm_service.api.routers.swarm_runs import _has_active_sandbox, stream_sandbox_logs
from agent_swarm_service.app import create_app
from agent_swarm_service.auth.session_store import InMemoryRunSecretStore, build_run_secret
from agent_swarm_service.config import ServiceSettings
from agent_swarm_service.dependencies import get_sandbox_client, get_swarm_run_service
from agent_swarm_service.orchestration.models import (
    ModelSelection,
    PlanFeedbackSubmission,
    RunOwner,
    SwarmActivitySummary,
    SwarmAgentSettings,
    SwarmMergeResolverSandbox,
    SwarmOptions,
    SwarmPlanState,
    SwarmRunState,
    SwarmTaskState,
)
from agent_swarm_service.orchestration.projections import build_event_snapshot
from agent_swarm_service.orchestration.sandbox_execution import (
    AcaSandboxLifecycleExecutor,
    MergeBranchInput,
    PlannerExecutionResult,
    PlannerSandboxActivityInput,
    SandboxLifecycleError,
)
from agent_swarm_service.sandboxes.workspace import (
    DEFAULT_LOG_MIRROR_PATH,
    WorkspaceFile,
    WorkspaceSnapshot,
    stage_snapshot,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
SANDBOX_EXECUTION_SOURCE = (
    REPO_ROOT / "src" / "agent_swarm_service" / "orchestration" / "sandbox_execution.py"
).read_text(encoding="utf-8")
ACA_CLIENT_SOURCE = (
    REPO_ROOT / "src" / "agent_swarm_service" / "sandboxes" / "aca_client.py"
).read_text(encoding="utf-8")
APP_SOURCE = (
    REPO_ROOT / "src" / "agent_swarm_service" / "app.py"
).read_text(encoding="utf-8")
SAMPLE_SANDBOX_RUNNER_SOURCE = (
    REPO_ROOT / "sandbox-image" / "run-role.py"
).read_text(encoding="utf-8")


def _run(coro):
    import asyncio

    return asyncio.run(coro)


@contextmanager
def _session_client(*, swarm_run_service=None, sandbox_client=None):
    try:
        from fastapi.testclient import TestClient
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise unittest.SkipTest("FastAPI test client is unavailable.") from exc

    app = create_app(ServiceSettings.for_local_development())
    if swarm_run_service is not None:
        app.dependency_overrides[get_swarm_run_service] = lambda: swarm_run_service
    if sandbox_client is not None:
        app.dependency_overrides[get_sandbox_client] = lambda: sandbox_client

    with TestClient(app) as client:
        yield client

    app.dependency_overrides.clear()


def _owner() -> RunOwner:
    return RunOwner(session_id="session-101")


def _options() -> SwarmOptions:
    return SwarmOptions(
        models=SwarmAgentSettings(
            planner=ModelSelection(model="claude-opus-4.6"),
            worker=ModelSelection(model="gpt-5.3-codex"),
            reviewer=ModelSelection(model="claude-opus-4.6"),
        )
    )


def _make_run(
    run_id: str,
    *,
    runtime_status: str = "Running",
    status: str = "running",
    phase: str = "Executing",
    active_task_sandbox_id: str | None = None,
    active_planner_sandbox_id: str | None = None,
    active_reviewer_sandbox_id: str | None = None,
    merge_resolver_sandboxes: list[SwarmMergeResolverSandbox] | None = None,
) -> SwarmRunState:
    task = SwarmTaskState(
        id="task-1",
        title="Validate SSE truth",
        status="Executing" if active_task_sandbox_id else "Completed",
        summary="Track the active sandbox from DTS projection state.",
        active_sandbox_id=active_task_sandbox_id,
    )
    return SwarmRunState(
        id=run_id,
        owner=_owner(),
        title="Validate SSE truth",
        prompt="Use DTS as the storage/orchestration layer like the .NET version.",
        repository_url="https://github.com/octo/repo",
        target_branch=f"swarm/{run_id}",
        runtime_status=runtime_status,
        status=status,
        phase=phase,
        options=_options(),
        plan=SwarmPlanState(design_document="Project DTS status truth through SSE.", tasks=[task]),
        tasks=[task],
        planner_summaries=[
            SwarmActivitySummary(id="planner-1", kind="planner", title="Planner", status="Completed", summary="Planned with DTS state.")
        ],
        worker_summaries=[
            SwarmActivitySummary(id="worker-1", kind="worker", title="Worker", status=task.status, summary="Worker sandbox in flight.")
        ],
        reviewer_summaries=[
            SwarmActivitySummary(id="review-1", kind="reviewer", title="Reviewer", status="Completed", summary="Reviewer published status truth.", publish_status="Published")
        ],
        active_planner_sandbox_id=active_planner_sandbox_id,
        active_reviewer_sandbox_id=active_reviewer_sandbox_id,
        merge_resolver_sandboxes=merge_resolver_sandboxes or [],
        publish_status="Published" if runtime_status == "Completed" else None,
    )


class _FakeSandboxClient:
    def __init__(self, contents: dict[tuple[str, str, str], bytes]) -> None:
        self.contents = contents
        self.requests: list[tuple[str, str, str, str | None]] = []

    def read_file(
        self,
        sandbox_id: str,
        sandbox_group: str,
        path: str,
        container_name: str | None = None,
        resource_group: str | None = None,
    ) -> bytes:
        del container_name
        self.requests.append((sandbox_id, sandbox_group, path, resource_group))
        return self.contents[(sandbox_id, sandbox_group, path)]


class _RoleExecSandboxClient:
    def __init__(self) -> None:
        self.contents: dict[tuple[str, str, str], bytes] = {}
        self.exec_calls: list[str] = []

    def mkdir(self, sandbox_id: str, sandbox_group: str, path: str, resource_group: str | None = None) -> None:
        del sandbox_id, sandbox_group, path, resource_group

    def write_file(
        self,
        sandbox_id: str,
        sandbox_group: str,
        path: str,
        content: str | bytes,
        resource_group: str | None = None,
    ) -> None:
        del resource_group
        payload = content.encode("utf-8") if isinstance(content, str) else content
        self.contents[(sandbox_id, sandbox_group, path)] = payload

    def exec(
        self,
        sandbox_id: str,
        sandbox_group: str,
        command: str,
        working_directory: str | None = None,
        resource_group: str | None = None,
    ) -> ExecResult:
        self.exec_calls.append(command)
        del sandbox_id, sandbox_group, working_directory, resource_group
        return ExecResult(exit_code=0, stdout="", stderr="")

    def read_file(
        self,
        sandbox_id: str,
        sandbox_group: str,
        path: str,
        resource_group: str | None = None,
    ) -> bytes:
        del resource_group
        return self.contents[(sandbox_id, sandbox_group, path)]


class _PlannerExecSandboxClient(_RoleExecSandboxClient):
    def __init__(self, result_payload: dict[str, object]) -> None:
        super().__init__()
        self.result_payload = result_payload

    def exec(
        self,
        sandbox_id: str,
        sandbox_group: str,
        command: str,
        working_directory: str | None = None,
        resource_group: str | None = None,
    ) -> ExecResult:
        del working_directory, resource_group
        self.exec_calls.append(command)
        result_root = command.split()[-1]
        self.contents[(sandbox_id, sandbox_group, f"{result_root}/result.json")] = json.dumps(
            self.result_payload,
            sort_keys=True,
        ).encode("utf-8")
        return ExecResult(exit_code=0, stdout="", stderr="")


class _FailingExecSandboxClient(_RoleExecSandboxClient):
    def __init__(self, *, stdout: str = "", stderr: str = "", log_text: str = "") -> None:
        super().__init__()
        self._stdout = stdout
        self._stderr = stderr
        self._log_text = log_text

    def exec(
        self,
        sandbox_id: str,
        sandbox_group: str,
        command: str,
        working_directory: str | None = None,
        resource_group: str | None = None,
    ) -> ExecResult:
        self.exec_calls.append(command)
        del working_directory, resource_group
        if self._log_text:
            self.contents[(sandbox_id, sandbox_group, DEFAULT_LOG_MIRROR_PATH)] = self._log_text.encode("utf-8")
        return ExecResult(exit_code=1, stdout=self._stdout, stderr=self._stderr)


class _TimeoutExecSandboxClient(_RoleExecSandboxClient):
    def exec(
        self,
        sandbox_id: str,
        sandbox_group: str,
        command: str,
        working_directory: str | None = None,
        resource_group: str | None = None,
    ) -> dict[str, object]:
        self.exec_calls.append(command)
        del sandbox_id, sandbox_group, working_directory, resource_group
        raise TimeoutError("The read operation timed out")


class _FakeSnapshotService:
    def __init__(self, snapshots) -> None:
        self._snapshots = snapshots if isinstance(snapshots, list) else [snapshots]
        self._call_count = 0

    async def get_event_snapshot(self, run_id: str):
        index = min(self._call_count, len(self._snapshots) - 1)
        self._call_count += 1
        snapshot = self._snapshots[index]
        if snapshot is None:
            return None
        return snapshot if run_id == snapshot.summary.id else None


class _FakeSwarmRunService(_FakeSnapshotService):
    async def get_run(self, run_id: str):
        snapshot = await self.get_event_snapshot(run_id)
        return None if snapshot is None else snapshot.summary

    async def get_plan(self, run_id: str):
        snapshot = await self.get_event_snapshot(run_id)
        return None if snapshot is None else snapshot.plan

    async def get_tasks(self, run_id: str):
        snapshot = await self.get_event_snapshot(run_id)
        return None if snapshot is None else snapshot.tasks

    async def get_details(self, run_id: str):
        snapshot = await self.get_event_snapshot(run_id)
        return None if snapshot is None else snapshot.details


def _parse_sse_events(body: str) -> list[tuple[str, object]]:
    events: list[tuple[str, object]] = []
    event_type: str | None = None
    payload: object | None = None
    for line in body.splitlines():
        if line.startswith("event: "):
            event_type = line.removeprefix("event: ")
        elif line.startswith("data: "):
            payload = json.loads(line.removeprefix("data: "))
        elif not line.strip() and event_type is not None:
            events.append((event_type, payload))
            event_type = None
            payload = None
    if event_type is not None:
        events.append((event_type, payload))
    return events


class SandboxRuntimeContractTests(unittest.TestCase):
    def test_runtime_source_does_not_allow_template_backed_or_harvested_execution_fallbacks(self) -> None:
        for forbidden in (
            "response-template.json",
            "apply-harvested-files.sh",
            "harvested-files.json",
            "build_harvested_files(",
            "build_harvested_file_content(",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, SANDBOX_EXECUTION_SOURCE)

    def test_runtime_wiring_does_not_expose_local_fake_sandbox_backend(self) -> None:
        for forbidden in (
            "LocalSandboxClient",
            "response-template.json",
            "harvested-files.json",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, ACA_CLIENT_SOURCE)
        self.assertNotIn("SandboxExecutionBackend.FAKE", APP_SOURCE)

    def test_run_secret_environment_includes_copilot_auth_alias_for_sandbox_runtime(self) -> None:
        environment = build_run_secret(
            "run-copilot-env",
            "ghp_example_token",
            lifetime=timedelta(hours=1),
        ).environment

        self.assertEqual(environment["COPILOT_GITHUB_TOKEN"], "ghp_example_token")

    def test_sample_sandbox_runner_uses_baked_copilot_runtime_in_process(self) -> None:
        self.assertIn("runpy.run_path(", SAMPLE_SANDBOX_RUNNER_SOURCE)
        self.assertIn("redirect_stdout(stdout)", SAMPLE_SANDBOX_RUNNER_SOURCE)

    def test_stage_snapshot_tolerates_precreated_workspace_root(self) -> None:
        class _WorkspaceAlreadyExistsClient(_RoleExecSandboxClient):
            def __init__(self) -> None:
                super().__init__()
                self.mkdir_calls: list[str] = []

            def mkdir(
                self,
                sandbox_id: str,
                sandbox_group: str,
                path: str,
                resource_group: str | None = None,
            ) -> None:
                del sandbox_id, sandbox_group, resource_group
                self.mkdir_calls.append(path)
                if path == "/workspace":
                    raise RuntimeError("directory already exists: /workspace")

        sandbox_client = _WorkspaceAlreadyExistsClient()

        _run(
            stage_snapshot(
                sandbox_client,
                "sandbox-1",
                "sandbox-group",
                WorkspaceSnapshot(files=[WorkspaceFile(path="note.txt", content="ok")]),
            )
        )

        self.assertEqual(
            sandbox_client.contents[("sandbox-1", "sandbox-group", "/workspace/note.txt")],
            b"ok",
        )
        self.assertEqual(sandbox_client.mkdir_calls, ["/workspace", "/workspace/.swarm"])

    def test_planner_runtime_request_stays_lean_and_excludes_dead_duplicate_fields(self) -> None:
        run = _make_run("swarm-planner-contract")
        run = run.model_copy(
            update={
                "plan_feedback_history": [
                    PlanFeedbackSubmission(
                        action="RequestChanges",
                        comments="Keep only the planner fields the runtime still consumes.",
                    )
                ],
                "pending_replan_summary": "Retry planning with the reviewer guidance folded in.",
                "pending_replan_findings": ["Do not pass duplicate branch metadata."],
            }
        )
        sandbox = {
            "sandbox_id": "planner-1",
            "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
            "resource_group": ServiceSettings.for_local_development().azure.resource_group,
        }
        sandbox_client = _PlannerExecSandboxClient(
            {
                "sandbox_id": "planner-1",
                "summary": "Planner built a lean runtime request.",
                "design_document": "Only retain the fields consumed by the planner runtime.",
                "tasks": [{"id": "task-1", "title": "Keep it lean"}],
            }
        )
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )

        result = _run(executor.execute_planner(PlannerSandboxActivityInput.model_validate({
            "sandbox": sandbox,
            "run_id": run.id,
            "created_at_utc": run.created_at_utc,
            "prompt": run.prompt,
            "repository_url": run.repository_url,
            "repository": {
                "host": "github.com",
                "owner": "octo",
                "name": "repo",
                "base_branch": "main",
            },
            "agent": {
                "model": run.options.models.planner.model,
                "copilot_runtime": run.options.copilot_runtime.model_dump(mode="json"),
            },
            "feedback": run.plan_feedback_history[-1].model_dump(mode="json"),
            "pending_replan_summary": run.pending_replan_summary,
            "pending_replan_findings": run.pending_replan_findings,
        })))

        request = json.loads(
            next(
                payload.decode("utf-8")
                for (_, _, path), payload in sandbox_client.contents.items()
                if path.endswith("request.json")
            )
        )
        self.assertEqual(
            set(request),
            {
                "agent",
                "feedback",
                "pendingReplanFindings",
                "pendingReplanSummary",
                "prompt",
                "repository",
                "repositoryUrl",
                "runId",
                "sandboxId",
            },
        )
        self.assertNotIn("baseBranch", request)
        self.assertNotIn("branchState", request)
        self.assertNotIn("run", request)
        self.assertNotIn("plan", request)
        self.assertNotIn("tasks", request)
        self.assertEqual(request["feedback"]["comments"], "Keep only the planner fields the runtime still consumes.")
        self.assertIsNone(result.tasks[0].branch_name)

    def test_worker_runtime_logs_exec_transport_failures_with_context(self) -> None:
        run = _make_run("swarm-worker-log-context")
        sandbox = {
            "sandbox_id": "worker-1",
            "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
            "resource_group": ServiceSettings.for_local_development().azure.resource_group,
        }
        sandbox_client = _TimeoutExecSandboxClient()
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )
        task = run.tasks[0]

        with self.assertLogs("agent_swarm_service.orchestration.sandbox_execution", level="INFO") as logs:
            with self.assertRaisesRegex(TimeoutError, "The read operation timed out"):
                _run(executor.execute_worker(run, task, sandbox))

        joined = "\n".join(logs.output)
        self.assertIn("Staging sandbox payload", joined)
        self.assertIn("Starting sandbox exec", joined)
        self.assertIn("Sandbox exec transport failed", joined)
        self.assertIn('"role": "worker"', joined)
        self.assertIn(f'"run_id": "{run.id}"', joined)
        self.assertIn(f'"task_id": "{task.id}"', joined)
        self.assertIn('"sandbox_id": "worker-1"', joined)

    def test_events_endpoint_streams_terminal_projection_snapshot(self) -> None:
        completed = _make_run("swarm-events", runtime_status="Completed", status="completed", phase="Completed")
        service = _FakeSwarmRunService(build_event_snapshot(completed))

        with _session_client(swarm_run_service=service) as client:
            response = client.get(f"/api/swarm-runs/{completed.id}/events")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/event-stream", response.headers["content-type"])
        events = _parse_sse_events(response.text)
        self.assertEqual([event for event, _ in events], ["status", "tasks", "plan", "details", "done"])
        payloads = {event: payload for event, payload in events}
        self.assertEqual(payloads["status"]["runtimeStatus"], "Completed")
        self.assertEqual(payloads["details"]["publishStatus"], "Published")
        self.assertEqual(len(payloads["tasks"]["tasks"]), 1)
        self.assertEqual(payloads["plan"]["designDocument"], "Project DTS status truth through SSE.")

    def test_worker_role_fails_fast_when_result_manifest_is_missing(self) -> None:
        run = _make_run("swarm-missing-result")
        sandbox_client = _RoleExecSandboxClient()
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )

        with self.assertRaisesRegex(SandboxLifecycleError, "did not produce a valid result artifact"):
            _run(
                executor._run_role(
                    "worker",
                    {
                        "sandbox_id": "worker-1",
                        "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
                        "resource_group": ServiceSettings.for_local_development().azure.resource_group,
                    },
                    {"sandboxId": "worker-1"},
                    environment={"GH_TOKEN": "ghp_example_token"},
                    result_payload={"summary": "should never be read"},
                )
            )

    def test_worker_role_surfaces_exec_output_in_failure_message(self) -> None:
        run = _make_run("swarm-worker-failure-output")
        sandbox_client = _FailingExecSandboxClient(stderr="RuntimeContractError: planner boom")
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )

        with self.assertRaisesRegex(SandboxLifecycleError, "RuntimeContractError: planner boom"):
            _run(
                executor._run_role(
                    "worker",
                    {
                        "sandbox_id": "worker-output-1",
                        "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
                        "resource_group": ServiceSettings.for_local_development().azure.resource_group,
                    },
                    {"sandboxId": "worker-output-1"},
                    environment={"GH_TOKEN": "ghp_example_token"},
                    result_payload={"summary": "should never be read"},
                )
            )

    def test_worker_role_falls_back_to_mirrored_log_when_exec_output_is_empty(self) -> None:
        run = _make_run("swarm-worker-failure-log")
        sandbox_client = _FailingExecSandboxClient(
            log_text="worker started\nRuntimeContractError: token ghp_example_token exploded\nworker failed\n"
        )
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )

        with self.assertRaises(SandboxLifecycleError) as exc:
            _run(
                executor._run_role(
                    "worker",
                    {
                        "sandbox_id": "worker-log-1",
                        "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
                        "resource_group": ServiceSettings.for_local_development().azure.resource_group,
                    },
                    {"sandboxId": "worker-log-1"},
                    environment={"GH_TOKEN": "ghp_example_token"},
                    result_payload={"summary": "should never be read"},
                )
            )

        message = str(exc.exception)
        self.assertIn("RuntimeContractError: token ghp_...oken exploded", message)
        self.assertNotIn("ghp_example_token", message)

    def test_planner_role_uses_baked_disk_image_runner_without_staging_runtime_files(self) -> None:
        run = _make_run("swarm-baked-planner")
        run = run.model_copy(
            update={
                "options": run.options.model_copy(
                    update={
                        "sandbox": run.options.sandbox.model_copy(
                            update={
                                "sandbox_disk_id": "/subscriptions/000/resourceGroups/rg/providers/Microsoft.App/diskImages/private-image"
                            }
                        )
                    }
                )
            }
        )
        sandbox_client = _PlannerExecSandboxClient(
            {
                "sandbox_id": "planner-baked-1",
                "summary": "Planner used the baked sandbox runner.",
                "design_document": "The runtime entrypoint came from the custom disk image.",
                "tasks": [{"id": "task-1", "title": "Keep it baked"}],
            }
        )
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )

        _run(
            executor.execute_planner(
                PlannerSandboxActivityInput.model_validate(
                    {
                        "sandbox": {
                            "sandbox_id": "planner-baked-1",
                            "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
                            "resource_group": ServiceSettings.for_local_development().azure.resource_group,
                            "labels": {
                                "runtime-contract": "baked-disk-image",
                            },
                        },
                        "run_id": run.id,
                        "created_at_utc": run.created_at_utc,
                        "prompt": run.prompt,
                        "repository_url": run.repository_url,
                        "repository": {
                            "host": "github.com",
                            "owner": "octo",
                            "name": "repo",
                            "base_branch": "main",
                        },
                        "agent": {
                            "model": run.options.models.planner.model,
                            "copilot_runtime": run.options.copilot_runtime.model_dump(mode="json"),
                        },
                    }
                )
            )
        )

        staged_paths = {path for (_, _, path), _ in sandbox_client.contents.items()}
        self.assertIn("/workspace/.swarm/request.json", staged_paths)
        self.assertIn(DEFAULT_LOG_MIRROR_PATH, staged_paths)
        self.assertNotIn("/workspace/.swarm/run-role.sh", staged_paths)
        self.assertNotIn("/workspace/.swarm/copilot_runtime.py", staged_paths)
        self.assertTrue(sandbox_client.exec_calls)
        self.assertIn("/opt/agent-swarm/run-role.py", sandbox_client.exec_calls[-1])

    def test_planner_request_payload_omits_dead_top_level_fields(self) -> None:
        run = _make_run("swarm-planner-payload").model_copy(
            update={
                "plan_feedback_history": [
                    PlanFeedbackSubmission(action="RequestChanges", comments="Tighten the execution wave.")
                ],
                "pending_replan_summary": "Carry the reviewer guidance into the next plan.",
                "pending_replan_findings": ["Preserve the current implementation context."],
            }
        )
        sandbox_client = _RoleExecSandboxClient()
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )
        captured: dict[str, object] = {}

        async def fake_run_role(role, sandbox, request_payload, *, environment, result_payload=None):
            del sandbox, environment, result_payload
            captured["role"] = role
            captured["payload"] = request_payload
            return {
                "sandbox_id": "planner-1",
                "summary": "Planner created a lean plan payload.",
                "design_document": "Drive planning from repository context plus review feedback.",
                "tasks": [{"id": "task-1", "title": "Plan", "summary": "Keep it lean."}],
            }

        with patch.object(executor, "_run_role", new=fake_run_role):
            _run(
                executor.execute_planner(
                    run,
                    {
                        "sandbox_id": "planner-1",
                        "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
                        "resource_group": ServiceSettings.for_local_development().azure.resource_group,
                    },
                )
            )

        payload = captured["payload"]
        self.assertEqual(captured["role"], "planner")
        self.assertNotIn("role", payload)
        self.assertNotIn("baseBranch", payload)
        self.assertNotIn("branchState", payload)
        self.assertEqual(payload["runId"], run.id)
        self.assertNotIn("targetBranch", payload)
        self.assertEqual(payload["repository"]["base_branch"], "main")
        self.assertEqual(payload["feedback"]["comments"], "Tighten the execution wave.")
        self.assertEqual(payload["pendingReplanFindings"], ["Preserve the current implementation context."])

    def test_planner_result_preserves_explicit_task_guidance(self) -> None:
        result = PlannerExecutionResult.model_validate(
            {
                "sandbox_id": "planner-docs-1",
                "summary": "Planner created a bounded docs task.",
                "design_document": "Keep README-only work scoped and actionable.",
                "tasks": [
                    {
                        "id": "task-docs-1",
                        "title": "Refresh README guidance",
                        "summary": "Clarify the README quickstart prerequisites.",
                        "target_files": ["README.md"],
                        "acceptance_criteria": ["README.md reflects the new quickstart prerequisites."],
                    }
                ],
            }
        )

        task = result.to_plan().tasks[0]
        self.assertEqual(task.target_files, ["README.md"])
        self.assertEqual(task.acceptance_criteria, ["README.md reflects the new quickstart prerequisites."])

    def test_worker_request_preserves_explicit_task_guidance(self) -> None:
        run = _make_run("swarm-worker-docs")
        task = run.tasks[0].model_copy(
            update={
                "title": "Refresh README quickstart guidance",
                "summary": "Clarify the README quickstart prerequisites and PAT-first auth flow.",
                "target_files": ["README.md"],
                "acceptance_criteria": ["README.md explains the PAT-first quickstart prerequisites."],
            }
        )
        sandbox = {
            "sandbox_id": "worker-docs-1",
            "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
            "resource_group": ServiceSettings.for_local_development().azure.resource_group,
        }
        sandbox_client = _RoleExecSandboxClient()
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )
        captured: dict[str, object] = {}

        async def fake_run_role(role, staged_sandbox, request_payload, *, environment, result_payload=None):
            del staged_sandbox, environment, result_payload
            captured["role"] = role
            captured["payload"] = request_payload
            return {
                "sandbox_id": "worker-docs-1",
                "summary": "Worker updated the README guidance.",
                "details": "README quickstart guidance now reflects the PAT-first flow.",
                "branch_name": request_payload["targetBranch"],
                "round_number": request_payload["task"]["round_number"],
                "head_commit_sha": "1" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["README.md"],
                "validation_summary": "No validation commands were configured.",
                "validation_results": [],
            }

        with patch.object(executor, "_run_role", new=fake_run_role):
            _run(executor.execute_worker(run, task, sandbox))

        payload = captured["payload"]["task"]
        self.assertEqual(captured["role"], "worker")
        self.assertEqual(payload["target_files"], ["README.md"])
        self.assertEqual(payload["acceptance_criteria"], ["README.md explains the PAT-first quickstart prerequisites."])

    def test_worker_request_does_not_infer_task_guidance(self) -> None:
        run = _make_run("swarm-worker-docs-no-infer")
        task = run.tasks[0].model_copy(
            update={
                "title": "Refresh README quickstart guidance",
                "summary": "Clarify the README quickstart prerequisites and PAT-first auth flow.",
                "target_files": [],
                "acceptance_criteria": [],
            }
        )
        sandbox = {
            "sandbox_id": "worker-docs-2",
            "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
            "resource_group": ServiceSettings.for_local_development().azure.resource_group,
        }
        sandbox_client = _RoleExecSandboxClient()
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )
        captured: dict[str, object] = {}

        async def fake_run_role(role, staged_sandbox, request_payload, *, environment, result_payload=None):
            del staged_sandbox, environment, result_payload
            captured["role"] = role
            captured["payload"] = request_payload
            return {
                "sandbox_id": "worker-docs-2",
                "summary": "Worker updated the README guidance.",
                "details": "README quickstart guidance now reflects the PAT-first flow.",
                "branch_name": request_payload["targetBranch"],
                "round_number": request_payload["task"]["round_number"],
                "head_commit_sha": "1" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["README.md"],
                "validation_summary": "No validation commands were configured.",
                "validation_results": [],
            }

        with patch.object(executor, "_run_role", new=fake_run_role):
            _run(executor.execute_worker(run, task, sandbox))

        payload = captured["payload"]["task"]
        self.assertEqual(captured["role"], "worker")
        self.assertNotIn("target_files", payload)
        self.assertNotIn("acceptance_criteria", payload)

    def test_worker_execution_accepts_explicit_no_change_result_contract(self) -> None:
        run = _make_run("swarm-worker-no-change")
        task = run.tasks[0].model_copy(update={"status": "Pending"})
        sandbox = {
            "sandbox_id": "worker-no-change-1",
            "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
            "resource_group": ServiceSettings.for_local_development().azure.resource_group,
        }
        sandbox_client = _RoleExecSandboxClient()
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )
        captured: dict[str, object] = {}

        async def fake_run_role(role, staged_sandbox, request_payload, *, environment, result_payload=None):
            del staged_sandbox, environment, result_payload
            captured["role"] = role
            captured["payload"] = request_payload
            return {
                "sandbox_id": "worker-no-change-1",
                "summary": "Worker confirmed the task was already satisfied.",
                "details": "Verified the repository state without creating a new commit.",
                "branch_name": request_payload["targetBranch"],
                "round_number": request_payload["task"]["round_number"],
                "head_commit_sha": "a" * 40,
                "parent_commit_sha": "a" * 40,
                "changed_files": [],
                "validation_summary": "No validation commands were requested.",
                "validation_results": [],
                "no_changes": True,
            }

        with patch.object(executor, "_run_role", new=fake_run_role):
            result = _run(executor.execute_worker(run, task, sandbox))

        self.assertEqual(captured["role"], "worker")
        self.assertEqual(captured["payload"]["targetBranch"], run.target_branch)
        self.assertEqual(captured["payload"]["task"]["id"], task.id)
        self.assertTrue(result.no_changes)
        self.assertEqual(result.changed_files, [])

    def test_reviewer_request_uses_integration_branch_head_after_fan_in(self) -> None:
        integration_branch = "swarm/octo/swarm-review-fanin/integration"
        run = _make_run("swarm-review-fanin")
        task_one = run.tasks[0].model_copy(
            update={
                "id": "task-1",
                "title": "Implement scheduling",
                "status": "InReview",
                "summary": "Worker task 1 is ready for review.",
                "branch_name": "swarm/octo/swarm-review-fanin/task-1-r1",
                "round_number": 1,
                "head_commit_sha": "1" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "validation_summary": "pytest task-1 passed",
                "validation_results": [],
            }
        )
        task_two = run.tasks[0].model_copy(
            update={
                "id": "task-2",
                "title": "Implement status updates",
                "status": "InReview",
                "summary": "Worker task 2 is ready for review.",
                "branch_name": "swarm/octo/swarm-review-fanin/task-2-r1",
                "round_number": 1,
                "head_commit_sha": "2" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["tests/test_dts_runtime.py"],
                "validation_summary": "pytest task-2 passed",
                "validation_results": [],
            }
        )
        tasks = [task_one, task_two]
        run = run.model_copy(
            update={
                "target_branch": integration_branch,
                "tasks": tasks,
                "plan": run.plan.model_copy(update={"tasks": tasks}),
                "branch_state": run.branch_state.model_copy(
                    update={
                        "branch_name": integration_branch,
                        "current_head_sha": "3" * 40,
                        "current_head_checkpoint_sequence": 4,
                    }
                ),
            }
        )
        sandbox = {
            "sandbox_id": "reviewer-fanin-1",
            "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
            "resource_group": ServiceSettings.for_local_development().azure.resource_group,
        }
        sandbox_client = _RoleExecSandboxClient()
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )
        captured: dict[str, object] = {}

        async def fake_run_role(role, staged_sandbox, request_payload, *, environment, result_payload=None):
            del staged_sandbox, environment, result_payload
            captured["role"] = role
            captured["payload"] = request_payload
            return {
                "sandbox_id": "reviewer-fanin-1",
                "outcome": "Approved",
                "summary": "Reviewer approved the integrated branch.",
                "details": "The fan-in branch is ready.",
                "findings": [],
                "fix_tasks": [],
                "replan_summary": None,
                "replan_findings": [],
                "target_branch": request_payload["targetBranch"],
                "pull_request_url": f"https://github.com/octo/repo/compare/main...{request_payload['targetBranch']}?expand=1",
            }

        with patch.object(executor, "_run_role", new=fake_run_role):
            result = _run(executor.execute_reviewer(run, sandbox))

        self.assertEqual(captured["role"], "reviewer")
        self.assertEqual(captured["payload"]["targetBranch"], integration_branch)
        self.assertEqual(captured["payload"]["branchState"]["branch_name"], integration_branch)
        self.assertEqual(captured["payload"]["branchState"]["current_head_sha"], "3" * 40)
        self.assertEqual(
            [task["id"] for task in captured["payload"]["completedTasks"]],
            ["task-1", "task-2"],
        )
        self.assertEqual(
            [task["branch_name"] for task in captured["payload"]["completedTasks"]],
            [
                "swarm/octo/swarm-review-fanin/task-1-r1",
                "swarm/octo/swarm-review-fanin/task-2-r1",
            ],
        )
        self.assertEqual(
            [task["round_number"] for task in captured["payload"]["completedTasks"]],
            [1, 1],
        )
        self.assertEqual(result.target_branch, integration_branch)

    def test_merge_request_preserves_completed_worker_branch_order(self) -> None:
        run = _make_run("swarm-merge-request")
        sandbox = {
            "sandbox_id": "merge-1",
            "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
            "resource_group": ServiceSettings.for_local_development().azure.resource_group,
        }
        sandbox_client = _RoleExecSandboxClient()
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )
        captured: dict[str, object] = {}

        async def fake_run_role(role, staged_sandbox, request_payload, *, environment, result_payload=None):
            del staged_sandbox, environment, result_payload
            captured["role"] = role
            captured["payload"] = request_payload
            return {
                "sandbox_id": "merge-1",
                "target_branch": request_payload["targetBranch"],
                "head_commit_sha": "c" * 40,
                "parent_commit_sha": "a" * 40,
                "merged_branch_names": [item["branch_name"] for item in request_payload["workerBranches"]],
                "deleted_branch_names": [item["branch_name"] for item in request_payload["workerBranches"]],
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "blocked": False,
                "blocked_reason": None,
            }

        worker_branches = [
            MergeBranchInput(
                task_id="task-1",
                branch_name="swarm/octo/swarm-merge-request/task-1-r1",
                head_commit_sha="1" * 40,
                parent_commit_sha="0" * 40,
                round_number=1,
                changed_files=["src/agent_swarm_service/orchestration/dts.py"],
            ),
            MergeBranchInput(
                task_id="task-2",
                branch_name="swarm/octo/swarm-merge-request/task-2-r1",
                head_commit_sha="2" * 40,
                parent_commit_sha="0" * 40,
                round_number=1,
                changed_files=["tests/test_dts_runtime.py"],
            ),
        ]

        with patch.object(executor, "_run_role", new=fake_run_role):
            result = _run(executor.execute_merge(run, worker_branches, sandbox))

        self.assertEqual(captured["role"], "merge")
        self.assertEqual(
            [item["task_id"] for item in captured["payload"]["workerBranches"]],
            ["task-1", "task-2"],
        )
        self.assertEqual(
            [item["branch_name"] for item in captured["payload"]["workerBranches"]],
            [
                "swarm/octo/swarm-merge-request/task-1-r1",
                "swarm/octo/swarm-merge-request/task-2-r1",
            ],
        )
        self.assertEqual(result.target_branch, run.target_branch)
        self.assertEqual(result.head_commit_sha, "c" * 40)
        self.assertEqual(result.parent_commit_sha, "a" * 40)
        self.assertEqual(
            result.deleted_branch_names,
            [
                "swarm/octo/swarm-merge-request/task-1-r1",
                "swarm/octo/swarm-merge-request/task-2-r1",
            ],
        )

    def test_merge_request_raises_when_runtime_reports_blocked_fan_in(self) -> None:
        run = _make_run("swarm-merge-blocked")
        sandbox = {
            "sandbox_id": "merge-blocked-1",
            "sandbox_group": ServiceSettings.for_local_development().azure.sandbox_group_name,
            "resource_group": ServiceSettings.for_local_development().azure.resource_group,
        }
        sandbox_client = _RoleExecSandboxClient()
        secret_store = InMemoryRunSecretStore()
        _run(secret_store.store(build_run_secret(run.id, "ghp_example_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=sandbox_client,
            run_secret_store=secret_store,
            run_secret_retry_delay_seconds=0.0,
        )

        async def fake_run_role(role, staged_sandbox, request_payload, *, environment, result_payload=None):
            del role, staged_sandbox, request_payload, environment, result_payload
            return {
                "sandbox_id": "merge-blocked-1",
                "target_branch": run.target_branch,
                "head_commit_sha": "b" * 40,
                "parent_commit_sha": "a" * 40,
                "merged_branch_names": [],
                "changed_files": [],
                "blocked": True,
                "blocked_reason": "Conflicts remained after Copilot resolution.",
            }

        worker_branches = [
            MergeBranchInput(
                task_id="task-1",
                branch_name="swarm/octo/swarm-merge-blocked/task-1-r1",
                head_commit_sha="1" * 40,
                parent_commit_sha="0" * 40,
                round_number=1,
                changed_files=["src/agent_swarm_service/orchestration/dts.py"],
            )
        ]

        with patch.object(executor, "_run_role", new=fake_run_role):
            with self.assertRaisesRegex(
                SandboxLifecycleError,
                "Conflicts remained after Copilot resolution.",
            ):
                _run(executor.execute_merge(run, worker_branches, sandbox))

    def test_logstream_route_streams_one_redacted_chunk_then_stops_when_snapshot_turns_terminal(self) -> None:
        active = build_event_snapshot(_make_run("swarm-logs", active_task_sandbox_id="sandbox-worker-1"))
        terminal = build_event_snapshot(
            _make_run("swarm-logs", runtime_status="Completed", status="completed", phase="Completed")
        )
        service = _FakeSwarmRunService([active, active, terminal])
        sandbox_client = _FakeSandboxClient(
            {
                (
                    "sandbox-worker-1",
                    ServiceSettings.for_local_development().azure.sandbox_group_name,
                    DEFAULT_LOG_MIRROR_PATH,
                ): (
                    b"worker started\n"
                    b"Bearer super-secret-token\n"
                    b"github_pat_abcdefghijklmnopqrstuvwxyz1234567890\n"
                ),
            }
        )

        with _session_client(swarm_run_service=service, sandbox_client=sandbox_client) as client:
            response = client.get(f"/api/swarm-runs/swarm-logs/sandboxes/sandbox-worker-1/logstream")

        self.assertEqual(response.status_code, 200)
        self.assertIn("text/plain", response.headers["content-type"])
        self.assertIn("worker started", response.text)
        self.assertIn("Bearer ***", response.text)
        self.assertNotIn("super-secret-token", response.text)
        self.assertNotIn("github_pat_abcdefghijklmnopqrstuvwxyz1234567890", response.text)
        self.assertEqual(len(sandbox_client.requests), 1)

    def test_logstream_route_rejects_stale_or_inactive_sandbox_ids(self) -> None:
        completed = build_event_snapshot(
            _make_run("swarm-stale", runtime_status="Completed", status="completed", phase="Completed")
        )
        service = _FakeSwarmRunService(completed)

        with self.assertRaises(HTTPException) as exc:
            _run(
                stream_sandbox_logs(
                    "swarm-stale",
                    "sandbox-worker-1",
                    settings=ServiceSettings.for_local_development(),
                    swarm_run_service=service,
                    sandbox_client=_FakeSandboxClient({}),
                )
            )

        self.assertEqual(exc.exception.status_code, 404)

    def test_has_active_sandbox_checks_task_reviewer_and_merge_resolver_sources(self) -> None:
        reviewer = build_event_snapshot(_make_run("swarm-reviewer", active_reviewer_sandbox_id="reviewer-1"))
        merge = build_event_snapshot(
            _make_run(
                "swarm-merge",
                merge_resolver_sandboxes=[
                    SwarmMergeResolverSandbox(branch_name="feature/conflict", round_number=2, sandbox_id="merge-1")
                ],
            )
        )
        worker = build_event_snapshot(_make_run("swarm-worker", active_task_sandbox_id="worker-1"))

        self.assertTrue(_has_active_sandbox(reviewer, "reviewer-1"))
        self.assertTrue(_has_active_sandbox(merge, "merge-1"))
        self.assertTrue(_has_active_sandbox(worker, "worker-1"))
        self.assertFalse(_has_active_sandbox(worker, "worker-stale"))


if __name__ == "__main__":
    unittest.main()
