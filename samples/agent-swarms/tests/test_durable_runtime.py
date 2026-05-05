from __future__ import annotations
 
import ast
import json
import unittest
from contextlib import contextmanager
from datetime import timedelta
from pathlib import Path
from unittest.mock import patch

import httpx
from pydantic import ValidationError

from contract_support import ensure_src_on_path

ensure_src_on_path()

from agent_swarm_service.api.schemas import CreateSwarmRunRequest
from agent_swarm_service.app import create_app
from agent_swarm_service.auth.session_store import InMemoryRunSecretStore, build_run_secret
from agent_swarm_service.config import DurableTaskSchedulerSettings, GitHubPublishBackend, ServiceSettings
from agent_swarm_service.dependencies import get_swarm_run_service
from agent_swarm_service.github.publishing import (
    DelegatedGitHubPublisher,
    GitHubPublishResult,
    create_github_publisher,
)
from agent_swarm_service.orchestration import dts as dts_module
from agent_swarm_service.orchestration.dts import (
    PLAN_REVIEW_EVENT_NAME,
    SwarmHistoryWindow,
    build_worker_registration,
    planning_instance_id,
)
from agent_swarm_service.orchestration.models import (
    ModelSelection,
    PlanFeedbackSubmission,
    RunOwner,
    SwarmAgentSettings,
    SwarmOptions,
    SwarmPlanState,
    SwarmRunState,
    SwarmTaskState,
)
from agent_swarm_service.orchestration.projections import build_projection_snapshot
from agent_swarm_service.orchestration.sandbox_execution import parse_repository_context
from agent_swarm_service.services.swarm_runs import SwarmRunService

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src" / "agent_swarm_service"

_SANDBOX_WRAPPER_DEFINITIONS = {
    "AcaSandboxClient",
    "InMemoryAcaSandboxClient",
    "SandboxAdapterError",
    "SandboxClientProtocol",
    "SandboxExecRequest",
    "SandboxExecResult",
    "SandboxFileEntry",
    "SandboxHandle",
    "SandboxSpec",
    "create_sandbox_client",
}
_SANDBOX_GROUP_WRAPPER_DEFINITIONS = {"SandboxGroupService"}
_WRAPPER_REFERENCES = _SANDBOX_WRAPPER_DEFINITIONS | _SANDBOX_GROUP_WRAPPER_DEFINITIONS


def _run(coro):
    import asyncio
 
    return asyncio.run(coro)


def _parse_module(relative_path: str) -> ast.AST:
    return ast.parse((SRC_ROOT / relative_path).read_text(encoding="utf-8"))


def _defined_names(tree: ast.AST) -> set[str]:
    return {
        node.name
        for node in ast.walk(tree)
        if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef))
    }


def _referenced_names(tree: ast.AST) -> set[str]:
    return {
        node.id
        for node in ast.walk(tree)
        if isinstance(node, ast.Name)
    }


@contextmanager
def _session_client(*, swarm_run_service):
    try:
        from fastapi.testclient import TestClient
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise unittest.SkipTest("FastAPI test client is unavailable.") from exc

    app = create_app(ServiceSettings.for_local_development())
    app.dependency_overrides[get_swarm_run_service] = lambda: swarm_run_service
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


@contextmanager
def _anonymous_session_clients(*, swarm_run_service):
    try:
        from fastapi.testclient import TestClient
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise unittest.SkipTest("FastAPI test client is unavailable.") from exc

    app = create_app(ServiceSettings.for_local_development())
    app.dependency_overrides[get_swarm_run_service] = lambda: swarm_run_service
    with TestClient(app) as owner_client, TestClient(app) as stranger_client:
        yield owner_client, stranger_client
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
    owner: RunOwner | None = None,
    runtime_status: str = "Running",
    status: str = "running",
    phase: str = "Planning",
    publish_status: str | None = None,
    active_task_sandbox_id: str | None = None,
) -> SwarmRunState:
    resolved_owner = owner or _owner()
    task = SwarmTaskState(
        id="task-1",
        title="Validate DTS cutover",
        status="Executing" if active_task_sandbox_id else "Completed",
        summary="Keep the public contract steady while the runtime seam changes.",
        active_sandbox_id=active_task_sandbox_id,
    )
    return SwarmRunState(
        id=run_id,
        owner=resolved_owner,
        title="Validate DTS cutover",
        prompt="Use DTS as the storage/orchestration layer like the .NET version.",
        repository_url="https://github.com/octo/repo",
        base_branch="main",
        target_branch=f"swarm/{run_id}",
        runtime_status=runtime_status,
        status=status,
        phase=phase,
        options=_options(),
        plan=SwarmPlanState(design_document="Drive the swarm through DTS orchestration state.", tasks=[task]),
        tasks=[task],
        publish_status=publish_status,
    )


class _FakeRuntimeCredential:
    def __init__(self, *args, **kwargs) -> None:
        self.args = args
        self.kwargs = kwargs


class _FakeRuntimeClient:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs


class _FakeRuntimeWorker:
    def __init__(self, **kwargs) -> None:
        self.kwargs = kwargs
        self.orchestrators: list[object] = []
        self.activities: list[object] = []

    def add_orchestrator(self, handler) -> None:
        self.orchestrators.append(handler)

    def add_activity(self, handler) -> None:
        self.activities.append(handler)

    def start(self) -> None:
        return None

    def stop(self) -> None:
        return None


class _FakeDtsBackend:
    def __init__(self, runs: list[SwarmRunState] | None = None) -> None:
        self.runs = {run.id: run for run in runs or []}
        self.projections = {}
        self.created_payloads: list[dict[str, object]] = []
        self.raised_events: list[tuple[str, str, PlanFeedbackSubmission]] = []
        self.suspended: list[tuple[str, str | None]] = []
        self.resumed: list[tuple[str, str | None]] = []
        self.cancelled: list[str] = []
        self.purged: list[str] = []
        self.rerun_requests: list[tuple[str, str]] = []

    async def list_runs(self) -> list[SwarmRunState]:
        return list(self.runs.values())

    async def create_run(
        self,
        *,
        owner: RunOwner,
        prompt: str,
        repository_url: str,
        base_branch: str | None,
        options: SwarmOptions,
        run_id: str | None = None,
    ) -> SwarmRunState:
        run = SwarmRunState(
            id=run_id or f"swarm-{len(self.created_payloads) + 1}",
            owner=owner,
            title="Validate DTS cutover",
            prompt=prompt,
            repository_url=repository_url,
            base_branch=base_branch,
            target_branch=f"swarm/{run_id or f'swarm-{len(self.created_payloads) + 1}'}",
            runtime_status="Pending",
            status="queued",
            phase="Queued",
            options=options,
        )
        self.created_payloads.append(
            {
                "owner": owner,
                "prompt": prompt,
                "repository_url": repository_url,
                "base_branch": base_branch,
                "options": options,
                "run_id": run.id,
            }
        )
        self.runs[run.id] = run
        return run

    async def get_run(self, run_id: str) -> SwarmRunState | None:
        return self.runs.get(run_id)

    async def get_projection(self, run_id: str):
        run = self.runs.get(run_id)
        if run is None:
            return None
        return self.projections.get(run_id)

    async def submit_plan_feedback(
        self,
        run_id: str,
        submission: PlanFeedbackSubmission,
    ) -> SwarmRunState | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        self.raised_events.append((planning_instance_id(run.owner.user_id, run_id), PLAN_REVIEW_EVENT_NAME, submission))
        updated = run.model_copy(update={"awaiting_plan_review": False})
        self.runs[run_id] = updated
        return updated

    async def request_suspend(self, run_id: str, reason: str | None):
        run = await self.get_run(run_id)
        if run is None:
            return None
        self.suspended.append((run_id, reason))
        updated = run.model_copy(update={"runtime_status": "Suspended", "status": "suspend_requested", "phase": "Suspended"})
        self.runs[run_id] = updated
        return updated

    async def request_resume(self, run_id: str, reason: str | None):
        run = await self.get_run(run_id)
        if run is None:
            return None
        self.resumed.append((run_id, reason))
        updated = run.model_copy(update={"runtime_status": "Running", "status": "running", "phase": "Planning"})
        self.runs[run_id] = updated
        return updated

    async def request_cancel(self, run_id: str):
        run = await self.get_run(run_id)
        if run is None:
            return None
        self.cancelled.append(run_id)
        updated = run.model_copy(update={"runtime_status": "Terminated", "status": "cancel_requested", "phase": "Cancelled"})
        self.runs[run_id] = updated
        return updated

    async def purge_run(self, run_id: str) -> bool:
        run = await self.get_run(run_id)
        if run is None:
            return False
        self.purged.append(run_id)
        del self.runs[run_id]
        self.projections.pop(run_id, None)
        return True

    async def rerun(self, run_id: str, owner: RunOwner, *, new_run_id: str | None = None):
        original = await self.get_run(run_id)
        if original is None:
            return None
        resolved_run_id = new_run_id or f"{run_id}-rerun"
        self.rerun_requests.append((run_id, resolved_run_id))
        rerun = original.model_copy(
            update={
                "id": resolved_run_id,
                "owner": owner,
                "runtime_status": "Pending",
                "status": "queued",
                "phase": "Queued",
                "publish_status": None,
            }
        )
        self.runs[rerun.id] = rerun
        return rerun


class DurableRuntimeTests(unittest.TestCase):
    def test_dts_settings_require_explicit_connection_string_instead_of_synthesizing_a_local_default(self) -> None:
        unresolved = DurableTaskSchedulerSettings.from_mapping({})
        configured = DurableTaskSchedulerSettings.from_mapping(
            {"DTS_CONNECTION_STRING": "Endpoint=https://scheduler.westus2.durabletask.io;Authentication=ManagedIdentity;ClientID=test-client;TaskHub=agent-swarm"}
        )

        self.assertIsNone(unresolved.connection_string)
        self.assertIn("scheduler.westus2.durabletask.io", configured.connection_string.get_secret_value())
        self.assertIn("TaskHub=agent-swarm", configured.connection_string.get_secret_value())

    def test_sandbox_modules_do_not_define_repo_local_sdk_wrappers_or_import_fallbacks(self) -> None:
        sandbox_tree = _parse_module("sandboxes\\aca_client.py")
        sandbox_group_tree = _parse_module("sandboxes\\sandbox_groups.py")
        sandbox_source = (SRC_ROOT / "sandboxes" / "aca_client.py").read_text(encoding="utf-8")
        sandbox_group_source = (SRC_ROOT / "sandboxes" / "sandbox_groups.py").read_text(encoding="utf-8")

        self.assertFalse(
            _defined_names(sandbox_tree) & _SANDBOX_WRAPPER_DEFINITIONS,
            "Direct SDK cutover should remove repo-local ACA wrapper classes, protocol types, and client factories.",
        )
        self.assertFalse(
            _defined_names(sandbox_group_tree) & _SANDBOX_GROUP_WRAPPER_DEFINITIONS,
            "Direct SDK cutover should remove the sandbox group wrapper service.",
        )
        self.assertNotIn("ImportError", sandbox_source)
        self.assertNotIn("ImportError", sandbox_group_source)
        self.assertNotIn("is_available", sandbox_source)

    def test_runtime_layers_stop_referencing_repo_local_sandbox_wrapper_contracts(self) -> None:
        module_paths = (
            "app.py",
            "dependencies.py",
            "sandboxes\\workspace.py",
            "orchestration\\dts.py",
            "orchestration\\sandbox_execution.py",
        )

        for relative_path in module_paths:
            with self.subTest(module=relative_path):
                self.assertFalse(
                    _referenced_names(_parse_module(relative_path)) & _WRAPPER_REFERENCES,
                    f"{relative_path} should use direct ACA/DTS SDK contracts instead of repo-local wrapper types.",
                )

    def test_publish_runtime_source_is_branch_native_and_does_not_fall_back_to_harvested_or_simulated_data(self) -> None:
        publishing_source = (SRC_ROOT / "github" / "publishing.py").read_text(encoding="utf-8")
        dts_source = (SRC_ROOT / "orchestration" / "dts.py").read_text(encoding="utf-8")

        for forbidden in (
            "_collect_harvested_file_changes",
            "_commit_harvested_files",
            "Published harvested file changes",
            "Deterministic local publish",
            "\"Simulated\"",
        ):
            with self.subTest(forbidden=forbidden):
                self.assertNotIn(forbidden, publishing_source + "\n" + dts_source)

    def test_publish_result_contract_rejects_retired_synthetic_publish_fields(self) -> None:
        with self.assertRaises(ValidationError):
            GitHubPublishResult.model_validate(
                {
                    "status": "Published",
                    "target_branch": "swarm/octo/run-contract/integration",
                    "commit_sha": "a" * 40,
                    "published_file_paths": ["src/agent_swarm_service/orchestration/dts.py"],
                }
            )

    def test_runtime_host_registers_native_dts_handlers_instead_of_legacy_loop_shim(self) -> None:
        registration = build_worker_registration()
        worker_instances: list[_FakeRuntimeWorker] = []

        def _worker_factory(**kwargs):
            worker = _FakeRuntimeWorker(**kwargs)
            worker_instances.append(worker)
            return worker

        with (
            patch.object(dts_module, "DefaultAzureCredential", _FakeRuntimeCredential),
            patch.object(dts_module, "DurableTaskSchedulerClient", _FakeRuntimeClient),
            patch.object(dts_module, "DurableTaskSchedulerWorker", _worker_factory),
        ):
            dts_module.DtsSwarmRuntimeHost(
                ServiceSettings.model_validate(
                    {
                        **ServiceSettings.for_local_development().model_dump(mode="json"),
                        "dts": {
                            "connection_string": "Endpoint=https://scheduler.westus2.durabletask.io;Authentication=ManagedIdentity;ClientID=test-client;TaskHub=agent-swarm",
                            "worker_enabled": False,
                        },
                        "orchestration": {"backend": "dts"},
                    }
                ),
                sandbox_lifecycle=object(),
                sandbox_client=object(),
                publish_service=object(),
            )

        self.assertEqual(len(worker_instances), 1)
        worker = worker_instances[0]
        self.assertEqual(tuple(handler.__name__ for handler in worker.orchestrators), registration.orchestrators)
        registered_activity_names = tuple(handler.__name__ for handler in worker.activities)
        self.assertEqual(registered_activity_names, registration.activities)
        self.assertNotIn("swarm_orchestration", tuple(handler.__name__ for handler in worker.orchestrators))
        self.assertNotIn("advance_run_activity", tuple(handler.__name__ for handler in worker.activities))
        self.assertNotIn("CreateSandboxActivity", registered_activity_names)
        self.assertNotIn("ExecuteSandboxRoleActivity", registered_activity_names)
        self.assertNotIn("CleanupSandboxesActivity", registered_activity_names)

    def test_api_routes_schedule_and_query_dts_backed_runs(self) -> None:
        backend = _FakeDtsBackend()
        run_secret_store = InMemoryRunSecretStore()
        service = SwarmRunService(ServiceSettings.for_local_development(), backend, run_secret_store)

        with _session_client(swarm_run_service=service) as client:
            create = client.post(
                "/api/swarm-runs",
                json={
                    "prompt": "Validate DTS runtime",
                    "repositoryUrl": "https://github.com/octo/repo",
                    "githubPat": "ghp_test_token",
                    "baseBranch": "main",
                },
            )
            created = create.json()
            listing = client.get("/api/swarm-runs")
            summary = client.get(f"/api/swarm-runs/{created['id']}")
            details = client.get(f"/api/swarm-runs/{created['id']}/details")

        self.assertEqual(create.status_code, 201)
        self.assertEqual(created["status"], "queued")
        self.assertEqual(backend.created_payloads[0]["repository_url"], "https://github.com/octo/repo")
        self.assertEqual(backend.created_payloads[0]["base_branch"], "main")
        self.assertEqual(backend.created_payloads[0]["run_id"], created["id"])
        self.assertIsNotNone(_run(run_secret_store.get(created["id"])))
        self.assertEqual(listing.status_code, 200)
        self.assertEqual(len(listing.json()), 1)
        self.assertEqual(summary.json()["id"], created["id"])
        self.assertEqual(details.json()["repositoryUrl"], "https://github.com/octo/repo")
        self.assertNotIn("githubPat", create.text)
        self.assertNotIn("github_pat", create.text)

    def test_api_routes_access_runs_by_id_without_session_cookies(self) -> None:
        backend = _FakeDtsBackend()
        service = SwarmRunService(ServiceSettings.for_local_development(), backend, InMemoryRunSecretStore())

        with _anonymous_session_clients(swarm_run_service=service) as (owner_client, stranger_client):
            create = owner_client.post(
                "/api/swarm-runs",
                json={
                    "prompt": "Validate anonymous ownership",
                    "repositoryUrl": "https://github.com/octo/repo",
                    "githubPat": "ghp_owner_token",
                },
            )
            created = create.json()
            owner_listing = owner_client.get("/api/swarm-runs")
            stranger_listing = stranger_client.get("/api/swarm-runs")
            stranger_summary = stranger_client.get(f"/api/swarm-runs/{created['id']}")

        self.assertEqual(create.status_code, 201)
        self.assertNotIn("swarm_session", owner_client.cookies)
        self.assertNotIn("swarm_session", stranger_client.cookies)
        self.assertEqual([item["id"] for item in owner_listing.json()], [created["id"]])
        self.assertEqual([item["id"] for item in stranger_listing.json()], [created["id"]])
        self.assertEqual(stranger_summary.status_code, 200)
        self.assertEqual(stranger_summary.json()["id"], created["id"])

    def test_api_control_routes_map_to_dts_client_operations(self) -> None:
        run = _make_run("swarm-control", phase="Planning")
        backend = _FakeDtsBackend([run])
        run_secret_store = InMemoryRunSecretStore()
        _run(run_secret_store.store(build_run_secret(run.id, "ghp_existing_token", lifetime=timedelta(hours=1))))
        service = SwarmRunService(ServiceSettings.for_local_development(), backend, run_secret_store)

        with _session_client(swarm_run_service=service) as client:
            feedback = client.post(
                f"/api/swarm-runs/{run.id}/plan/feedback",
                json={"action": "Approved", "comments": "Looks good.", "revisedTasks": []},
            )
            suspend = client.post(f"/api/swarm-runs/{run.id}/suspend?reason=Pause")
            resume = client.post(f"/api/swarm-runs/{run.id}/resume?reason=Continue")
            rerun = client.post(f"/api/swarm-runs/{run.id}/rerun")
            cancel = client.delete(f"/api/swarm-runs/{run.id}")
            purge = client.delete(f"/api/swarm-runs/{run.id}/purge")

        self.assertEqual(feedback.status_code, 202)
        self.assertEqual(backend.raised_events[0][0], planning_instance_id(_owner().session_id, run.id))
        self.assertEqual(backend.raised_events[0][1], PLAN_REVIEW_EVENT_NAME)
        self.assertEqual(backend.raised_events[0][2].action.value, "Approved")
        self.assertEqual(suspend.status_code, 200)
        self.assertEqual(backend.suspended, [(run.id, "Pause")])
        self.assertEqual(resume.status_code, 200)
        self.assertEqual(backend.resumed, [(run.id, "Continue")])
        self.assertEqual(rerun.status_code, 201)
        rerun_id = rerun.json()["id"]
        self.assertNotEqual(rerun_id, run.id)
        self.assertIsNotNone(_run(run_secret_store.get(rerun_id)))
        self.assertEqual(backend.rerun_requests, [(run.id, rerun_id)])
        self.assertEqual(cancel.status_code, 200)
        self.assertEqual(backend.cancelled, [run.id])
        self.assertEqual(purge.status_code, 200)
        self.assertEqual(_run(run_secret_store.get(run.id)), None)
        self.assertEqual(backend.purged, [run.id])

    def test_service_stores_secret_before_coordinator_schedules_run(self) -> None:
        class _VerifyingBackend(_FakeDtsBackend):
            def __init__(self, store: InMemoryRunSecretStore) -> None:
                super().__init__()
                self._store = store

            async def create_run(self, **kwargs) -> SwarmRunState:
                run_id = kwargs["run_id"]
                secret = await self._store.get(run_id)
                if secret is None:
                    raise AssertionError("Run secret was missing when create_run executed.")
                return await super().create_run(**kwargs)

        run_secret_store = InMemoryRunSecretStore()
        backend = _VerifyingBackend(run_secret_store)
        service = SwarmRunService(ServiceSettings.for_local_development(), backend, run_secret_store)

        created = _run(
            service.create_run(
                CreateSwarmRunRequest(
                    prompt="Validate ordering",
                    repository_url="https://github.com/octo/repo",
                    github_pat="ghp_test_token",
                ),
            )
        )

        self.assertEqual(backend.created_payloads[0]["run_id"], created.id)

    def test_service_rejects_run_creation_without_resolved_private_disk_id(self) -> None:
        settings = ServiceSettings.from_env(
            {
                "SWARM_APP_BASE_URL": "https://swarm.example.com",
                "DTS_CONNECTION_STRING": "Endpoint=https://scheduler.example.com;Authentication=ManagedIdentity;TaskHub=swarm",
                "AZURE_SUBSCRIPTION_ID": "00000000-0000-0000-0000-000000000000",
                "AZURE_RESOURCE_GROUP": "rg-swarm",
                "AZURE_LOCATION": "westus2",
                "SWARM_STORAGE_ACCOUNT_URL": "https://storage.blob.core.windows.net/",
                "SWARM_SANDBOX_GROUP_NAME": "swarm-sandbox-group",
            }
        )
        backend = _FakeDtsBackend()
        service = SwarmRunService(settings, backend, InMemoryRunSecretStore())

        with self.assertRaises(ValueError) as exc:
            _run(
                service.create_run(
                    CreateSwarmRunRequest(
                        prompt="Validate DiskId requirement",
                        repository_url="https://github.com/octo/repo",
                        github_pat="ghp_test_token",
                    ),
                )
            )

        self.assertIn("diskid", str(exc.exception).lower().replace(" ", ""))
        self.assertEqual(backend.created_payloads, [])

    def test_service_stores_rerun_secret_before_coordinator_schedules_rerun(self) -> None:
        class _VerifyingBackend(_FakeDtsBackend):
            def __init__(self, runs: list[SwarmRunState], store: InMemoryRunSecretStore) -> None:
                super().__init__(runs)
                self._store = store

            async def rerun(self, run_id: str, owner: RunOwner, *, new_run_id: str | None = None):
                secret = await self._store.get(new_run_id)
                if secret is None:
                    raise AssertionError("Rerun secret was missing when rerun executed.")
                return await super().rerun(run_id, owner, new_run_id=new_run_id)

        original = _make_run("swarm-rerun-ordering")
        run_secret_store = InMemoryRunSecretStore()
        _run(run_secret_store.store(build_run_secret(original.id, "ghp_existing_token", lifetime=timedelta(hours=1))))
        backend = _VerifyingBackend([original], run_secret_store)
        service = SwarmRunService(ServiceSettings.for_local_development(), backend, run_secret_store)

        rerun = _run(service.rerun(original.id))

        self.assertIsNotNone(rerun)
        self.assertEqual(backend.rerun_requests, [(original.id, rerun.id)])

    def test_event_snapshot_prefers_dts_projection_for_publish_and_task_truth(self) -> None:
        stale = _make_run("swarm-projection", phase="Reviewing", publish_status=None)
        projected = stale.model_copy(update={"publish_status": "Published"})
        projected.tasks[0].active_sandbox_id = "sandbox-worker-1"
        backend = _FakeDtsBackend([stale])
        backend.projections[stale.id] = build_projection_snapshot(projected)
        service = SwarmRunService(ServiceSettings.for_local_development(), backend, InMemoryRunSecretStore())

        snapshot = _run(service.get_event_snapshot(stale.id))

        self.assertIsNotNone(snapshot)
        self.assertEqual(snapshot.details.publish_status, "Published")
        self.assertEqual(snapshot.tasks.tasks[0].active_sandbox_id, "sandbox-worker-1")
        self.assertIsNone(snapshot.summary.active_reviewer_sandbox_id)

    def test_continue_as_new_history_bounding_preserves_visible_run_state(self) -> None:
        history = SwarmHistoryWindow(
            title="Validate DTS cutover",
            publish_status="Queued",
            task_ids=["task-1", "task-2"],
            pending_replan_summary="Tighten the orchestration loop.",
            pending_replan_findings=["Carry forward reviewer findings."],
        )

        after_first = history.record_round()
        after_second = after_first.record_round()

        self.assertTrue(after_second.should_continue_as_new(2))
        compacted = after_second.continue_as_new()
        self.assertEqual(compacted.total_execution_rounds, 2)
        self.assertEqual(compacted.rounds_since_continue_as_new, 0)
        self.assertEqual(compacted.title, "Validate DTS cutover")
        self.assertEqual(compacted.publish_status, "Queued")
        self.assertEqual(compacted.task_ids, ["task-1", "task-2"])
        self.assertEqual(compacted.pending_replan_summary, "Tighten the orchestration loop.")
        self.assertEqual(compacted.pending_replan_findings, ["Carry forward reviewer findings."])

    def test_publish_fails_before_github_io_when_branch_head_is_missing_even_if_task_metadata_exists(self) -> None:
        def _unexpected_request(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                f"Publish should fail before any GitHub request when current head metadata is missing, got {request.method} {request.url!s}."
            )

        run = _make_run("swarm-publish-preconditions", runtime_status="Running", status="running", phase="Publishing")
        run.tasks[0].status = "Completed"
        run.tasks[0].branch_name = "swarm/octo/swarm-publish-preconditions/integration"
        run.tasks[0].head_commit_sha = "f" * 40
        run.tasks[0].parent_commit_sha = "e" * 40
        run.tasks[0].changed_files = ["src/agent_swarm_service/orchestration/dts.py"]
        run.tasks[0].validation_summary = "pytest -q failed with 2 regressions"
        run_secret_store = InMemoryRunSecretStore()
        _run(run_secret_store.store(build_run_secret(run.id, "ghp_publish_token", lifetime=timedelta(hours=1))))
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_unexpected_request),
            base_url="https://api.github.com/",
        )
        publisher = DelegatedGitHubPublisher(ServiceSettings.for_local_development(), run_secret_store, client=client)

        with self.assertRaisesRegex(RuntimeError, "current head|branch head|commit"):
            _run(
                publisher.publish_run(
                    run,
                    repo=parse_repository_context(run.repository_url, run.base_branch),
                    target_branch=run.target_branch or "swarm/octo/swarm-publish-preconditions/integration",
                )
            )
        _run(client.aclose())

    def test_publish_run_stays_pinned_to_integration_branch_after_fan_in(self) -> None:
        integration_branch = "swarm/octo/swarm-publish-fanin/integration"
        worker_branch_1 = "swarm/octo/swarm-publish-fanin/task-1-r1"
        worker_branch_2 = "swarm/octo/swarm-publish-fanin/task-2-r1"
        integration_head = "c" * 40
        captured_requests: list[tuple[str, str]] = []
        captured_pull_request_payloads: list[dict[str, object]] = []

        def _mock_github(request: httpx.Request) -> httpx.Response:
            captured_requests.append((request.method, request.url.path))
            if request.method == "GET" and request.url.path.endswith(f"/git/ref/heads/{integration_branch}"):
                return httpx.Response(200, json={"object": {"sha": integration_head}})
            if request.method == "POST" and request.url.path.endswith("/pulls"):
                json_payload = json.loads(request.content.decode("utf-8"))
                captured_pull_request_payloads.append(json_payload)
                return httpx.Response(
                    201,
                    json={"html_url": "https://github.com/octo/repo/pull/321", "number": 321},
                )
            raise AssertionError(f"Unexpected GitHub request: {request.method} {request.url!s}")

        run = _make_run("swarm-publish-fanin", runtime_status="Running", status="running", phase="Publishing")
        run.target_branch = integration_branch
        run.branch_state.branch_name = integration_branch
        run.branch_state.current_head_sha = integration_head
        run.branch_state.reviewed_head_sha = integration_head
        run.branch_state.approved_branch_name = integration_branch
        run.branch_state.approved_head_sha = integration_head
        run.tasks = [
            SwarmTaskState(
                id="task-1",
                title="Integrate planner updates",
                status="Completed",
                branch_name=worker_branch_1,
                head_commit_sha="a" * 40,
                parent_commit_sha="0" * 40,
                changed_files=["src/agent_swarm_service/orchestration/dts.py"],
                validation_summary="pytest task-1 passed",
            ),
            SwarmTaskState(
                id="task-2",
                title="Integrate publish wiring",
                status="Completed",
                branch_name=worker_branch_2,
                head_commit_sha="b" * 40,
                parent_commit_sha="a" * 40,
                changed_files=["src/agent_swarm_service/github/publishing.py"],
                validation_summary="pytest task-2 passed",
            ),
        ]
        run.plan = run.plan.model_copy(update={"tasks": run.tasks})
        run_secret_store = InMemoryRunSecretStore()
        _run(run_secret_store.store(build_run_secret(run.id, "ghp_publish_token", lifetime=timedelta(hours=1))))
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_mock_github),
            base_url="https://api.github.com/",
        )
        publisher = DelegatedGitHubPublisher(ServiceSettings.for_local_development(), run_secret_store, client=client)

        result = _run(
            publisher.publish_run(
                run,
                repo=parse_repository_context(run.repository_url, run.base_branch),
                target_branch=integration_branch,
            )
        )

        self.assertEqual(result.target_branch, integration_branch)
        self.assertEqual(result.commit_sha, integration_head)
        self.assertEqual(captured_requests[0][0], "GET")
        self.assertIn(integration_branch, captured_requests[0][1])
        self.assertEqual(captured_pull_request_payloads[0]["head"], integration_branch)
        self.assertIn(f"- Integration branch: `{integration_branch}`", captured_pull_request_payloads[0]["body"])
        self.assertIn(worker_branch_1, captured_pull_request_payloads[0]["body"])
        self.assertIn(worker_branch_2, captured_pull_request_payloads[0]["body"])
        _run(client.aclose())

    def test_publish_run_rejects_worker_branch_after_fan_in(self) -> None:
        worker_branch = "swarm/octo/swarm-publish-fanin/task-2-r1"

        def _unexpected_request(request: httpx.Request) -> httpx.Response:
            raise AssertionError(
                f"Publish should reject worker task branches before any GitHub request, got {request.method} {request.url!s}."
            )

        run = _make_run("swarm-publish-fanin", runtime_status="Running", status="running", phase="Publishing")
        run.target_branch = "swarm/octo/swarm-publish-fanin/integration"
        run.branch_state.branch_name = run.target_branch
        run.branch_state.current_head_sha = "c" * 40
        run.tasks = [
            SwarmTaskState(id="task-1", title="Task 1", status="Completed", branch_name="swarm/octo/swarm-publish-fanin/task-1-r1", head_commit_sha="a" * 40),
            SwarmTaskState(id="task-2", title="Task 2", status="Completed", branch_name=worker_branch, head_commit_sha="b" * 40),
        ]
        run.plan = run.plan.model_copy(update={"tasks": run.tasks})
        run_secret_store = InMemoryRunSecretStore()
        _run(run_secret_store.store(build_run_secret(run.id, "ghp_publish_token", lifetime=timedelta(hours=1))))
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(_unexpected_request),
            base_url="https://api.github.com/",
        )
        publisher = DelegatedGitHubPublisher(ServiceSettings.for_local_development(), run_secret_store, client=client)

        with self.assertRaisesRegex(RuntimeError, "integration branch"):
            _run(
                publisher.publish_run(
                    run,
                    repo=parse_repository_context(run.repository_url, run.base_branch),
                    target_branch=worker_branch,
                )
            )
        _run(client.aclose())

    def test_create_github_publisher_only_exposes_delegated_runtime_publishers(self) -> None:
        run_secret_store = InMemoryRunSecretStore()
        settings = ServiceSettings.for_local_development()

        self.assertIsInstance(create_github_publisher(settings, run_secret_store), DelegatedGitHubPublisher)

        github_api_settings = ServiceSettings.model_validate(
            {
                **settings.model_dump(mode="json"),
                "runtime": {
                    **settings.runtime.model_dump(mode="json"),
                    "github_publish_backend": GitHubPublishBackend.GITHUB_API.value,
                },
            }
        )
        self.assertIsInstance(create_github_publisher(github_api_settings, run_secret_store), DelegatedGitHubPublisher)


if __name__ == "__main__":
    unittest.main()
