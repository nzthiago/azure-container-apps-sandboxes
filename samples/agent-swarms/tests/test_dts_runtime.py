from __future__ import annotations

import asyncio
import inspect
import os
import subprocess
import sys
import tempfile
import types
import unittest
from collections.abc import Mapping
from contextlib import contextmanager
from datetime import UTC, datetime, timedelta
from pathlib import Path
from unittest.mock import patch

from azure.containerapps.sandbox import Sandbox, SandboxClient
from durabletask.client import OrchestrationState, OrchestrationStatus
from durabletask.internal import orchestrator_service_pb2 as pb
from durabletask.task import CompletableTask, RetryPolicy, Task, TaskFailedError
from pydantic import ValidationError

REPO_ROOT = Path(__file__).resolve().parents[1]
SRC_ROOT = REPO_ROOT / "src"
if str(SRC_ROOT) not in sys.path:
    sys.path.insert(0, str(SRC_ROOT))

COPILOT_RUNTIME_SOURCE = (
    REPO_ROOT / "src" / "agent_swarm_service" / "orchestration" / "copilot_runtime.py"
).read_text(encoding="utf-8")

from agent_swarm_service.auth.session_store import InMemoryRunSecretStore, build_run_secret
from agent_swarm_service.config import ServiceSettings
from agent_swarm_service.orchestration.dts import (
    DtsConnectionInfo,
    DtsSwarmCoordinator,
    DurableRunOwnershipStore,
    EXECUTION_ROUND_ORCHESTRATION_NAME,
    GIT_MERGE_ACTIVITY_NAME,
    PLAN_REVIEW_EVENT_NAME,
    PLANNING_SUB_ORCHESTRATION_NAME,
    PUBLISH_TO_GITHUB_ACTIVITY_NAME,
    RUN_PLANNER_IN_SANDBOX_ACTIVITY_NAME,
    RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME,
    RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME,
    _make_git_merge_activity,
    _make_run_planner_activity,
    _make_run_review_activity,
    _make_run_worker_activity,
    build_worker_registration,
    execution_round_instance_id,
    execution_round_orchestration,
    planning_instance_id,
    planning_sub_orchestration,
    replan_instance_id,
    swarm_orchestration,
)
from agent_swarm_service.orchestration.models import (
    CoordinatorCheckpoint,
    HumanReviewMode,
    ModelSelection,
    PlanFeedbackSubmission,
    RunOwner,
    SwarmActivitySummary,
    SwarmAgentSettings,
    SwarmOptions,
    SwarmPlanState,
    SwarmPlanningSettings,
    SwarmRunState,
    SwarmTaskState,
    SwarmTaskStatusTransition,
)
from agent_swarm_service.orchestration.projections import build_projection_snapshot
from agent_swarm_service.orchestration.sandbox_execution import (
    AcaSandboxLifecycleExecutor,
    MergeExecutionResult,
    PlannerExecutionResult,
    ReviewerExecutionResult,
    WorkerExecutionResult,
)
from agent_swarm_service.runtime.storage import InMemoryRuntimeStorageBackend


def _sandbox_id_value(sandbox: object | None) -> str:
    if sandbox is None:
        return ""
    if isinstance(sandbox, Sandbox):
        return sandbox.id
    if isinstance(sandbox, Mapping):
        return str(sandbox.get("id") or "")
    return str(getattr(sandbox, "id", "") or "")


def _run(coro):
    return asyncio.run(coro)


def _git_command(cwd: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=cwd,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise AssertionError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed")
    return result


def _remote_branch_exists(remote_dir: Path, branch_name: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", f"refs/heads/{branch_name}"],
        cwd=remote_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


@contextmanager
def _worker_test_repo(target_branch: str):
    with tempfile.TemporaryDirectory(dir=REPO_ROOT) as temp_dir:
        root = Path(temp_dir)
        remote = root / "remote.git"
        repo = root / "repo"
        _git_command(REPO_ROOT, "init", "--bare", str(remote))
        _git_command(REPO_ROOT, "init", str(repo))
        _git_command(repo, "config", "user.name", "Agent Swarm")
        _git_command(repo, "config", "user.email", "agent-swarm@users.noreply.github.com")
        _git_command(repo, "checkout", "-b", "main")
        (repo / "README.md").write_text("initial\n", encoding="utf-8")
        _git_command(repo, "add", "README.md")
        _git_command(repo, "commit", "-m", "Initial commit")
        _git_command(repo, "remote", "add", "origin", str(remote))
        _git_command(repo, "push", "-u", "origin", "main")
        _git_command(repo, "checkout", "-b", target_branch)
        yield repo, remote


def _worker_request(repo_dir: Path, target_branch: str, *, validation_commands: list[str] | None = None) -> dict[str, object]:
    return {
        "sandboxId": "worker-1",
        "runId": "run-worker-direct-edit",
        "prompt": "Implement the worker task.",
        "targetBranch": target_branch,
        "repository": {
            "host": "github.com",
            "owner": "octo",
            "name": "repo",
            "base_branch": "main",
        },
        "branchState": {
            "current_head_sha": _git_command(repo_dir, "rev-parse", "HEAD").stdout.strip(),
        },
        "agent": {
            "model": "gpt-5",
            "copilot_runtime": {
                "provider": "github-copilot-sdk",
                "token_environment_variable": "COPILOT_GITHUB_TOKEN",
            },
        },
        "task": {
            "id": "task-1",
            "title": "Implement direct repo edits",
            "round_number": 1,
            "validation_commands": list(validation_commands or []),
        },
    }


def _merge_request(
    repo_dir: Path,
    target_branch: str,
    worker_branches: list[dict[str, object]],
) -> dict[str, object]:
    return {
        "sandboxId": "merge-1",
        "runId": "run-merge",
        "prompt": "Integrate completed worker branches.",
        "targetBranch": target_branch,
        "repository": {
            "host": "github.com",
            "owner": "octo",
            "name": "repo",
            "base_branch": "main",
        },
        "branchState": {
            "current_head_sha": _git_command(repo_dir, "rev-parse", "HEAD").stdout.strip(),
        },
        "agent": {
            "model": "gpt-5",
            "copilot_runtime": {
                "provider": "github-copilot-sdk",
                "token_environment_variable": "COPILOT_GITHUB_TOKEN",
            },
        },
        "workerBranches": worker_branches,
    }


def _owner() -> RunOwner:
    return RunOwner(session_id="session-101")


def _options() -> SwarmOptions:
    return SwarmOptions(
        planning=SwarmPlanningSettings(human_review_mode=HumanReviewMode.NONE),
        models=SwarmAgentSettings(
            planner=ModelSelection(model="claude-opus-4.6"),
            worker=ModelSelection(model="gpt-5.3-codex"),
            reviewer=ModelSelection(model="claude-opus-4.6"),
        ),
    )


class _FakeDtsClient:
    def __init__(self) -> None:
        self.scheduled: list[tuple] = []
        self.events: list[tuple] = []
        self.suspended: list[str] = []
        self.resumed: list[str] = []
        self.terminated: list[tuple[str, object | None]] = []
        self.purged: list[str] = []
        self.states: dict[str, OrchestrationState] = {}

    def schedule_new_orchestration(self, orchestrator, *, input=None, instance_id=None, tags=None, version=None, start_at=None, reuse_id_policy=None):
        self.scheduled.append((orchestrator, input, instance_id, tags))
        self.states[instance_id] = OrchestrationState(
            instance_id=instance_id,
            name=getattr(orchestrator, "__name__", str(orchestrator)),
            runtime_status=OrchestrationStatus.PENDING,
            created_at=datetime.now(UTC),
            last_updated_at=datetime.now(UTC),
            serialized_input=None,
            serialized_output=None,
            serialized_custom_status=None,
            failure_details=None,
        )
        return instance_id

    def get_orchestration_state(self, instance_id: str, *, fetch_payloads: bool = True):
        del fetch_payloads
        return self.states.get(instance_id)

    def raise_orchestration_event(self, instance_id: str, event_name: str, *, data=None):
        self.events.append((instance_id, event_name, data))

    def suspend_orchestration(self, instance_id: str):
        self.suspended.append(instance_id)

    def resume_orchestration(self, instance_id: str):
        self.resumed.append(instance_id)

    def terminate_orchestration(self, instance_id: str, *, output=None, recursive: bool = True):
        del recursive
        self.terminated.append((instance_id, output))

    def purge_orchestration(self, instance_id: str, recursive: bool = True):
        del recursive
        self.purged.append(instance_id)
        self.states.pop(instance_id, None)


def _task_failure_details(message: str, *, error_type: str = "RuntimeError") -> pb.TaskFailureDetails:
    details = pb.TaskFailureDetails()
    details.errorMessage = message
    details.errorType = error_type
    return details


class _FakeActivityTask(CompletableTask[object]):
    def __init__(self, record: dict[str, object]) -> None:
        super().__init__()
        self.record = record

    def __getitem__(self, key: str) -> object:
        return self.record[key]

    def get(self, key: str, default: object | None = None) -> object | None:
        return self.record.get(key, default)

    def __contains__(self, key: object) -> bool:
        return key in self.record

    def __repr__(self) -> str:
        return repr(self.record)

    def complete(self, result: object) -> None:
        self.record["is_complete"] = True
        self.record["result"] = result
        super().complete(result)

    def fail_with(self, message: str, *, error_type: str = "RuntimeError") -> None:
        self.record["is_complete"] = True
        self.record["is_failed"] = True
        self.record["failure_message"] = message
        try:
            self.fail(message, _task_failure_details(message, error_type=error_type))
        except TaskFailedError:
            pass


class _FakeOrchestrationContext:
    def __init__(self) -> None:
        self.calls: list[dict[str, object]] = []
        self.custom_statuses: list[dict[str, object]] = []

    def set_custom_status(self, status: dict[str, object]) -> None:
        self.custom_statuses.append(status)

    def call_sub_orchestrator(self, orchestrator, *, input=None, instance_id=None, retry_policy=None, version=None):
        del retry_policy, version
        record = {
            "kind": "sub_orchestrator",
            "name": getattr(orchestrator, "__name__", str(orchestrator)),
            "input": input,
            "instance_id": instance_id,
        }
        self.calls.append(record)
        return record

    def call_activity(self, activity, *, input=None, retry_policy=None):
        record = {
            "kind": "activity",
            "name": getattr(activity, "__name__", str(activity)),
            "input": input,
            "retry_policy": retry_policy,
        }
        task = _FakeActivityTask(record)
        record["task"] = task
        self.calls.append(record)
        return task

    def wait_for_external_event(self, name: str):
        record = {"kind": "event", "name": name}
        self.calls.append(record)
        return record


def _activity_group(yielded_task: Task[object]) -> list[_FakeActivityTask]:
    scheduled = list(yielded_task.get_tasks()) if hasattr(yielded_task, "get_tasks") else [yielded_task]
    if not all(isinstance(task, _FakeActivityTask) for task in scheduled):
        raise AssertionError(f"Expected fake activity tasks, got {scheduled!r}.")
    return list(scheduled)


def _single_activity(yielded_task: Task[object]) -> _FakeActivityTask:
    scheduled = _activity_group(yielded_task)
    if len(scheduled) != 1:
        raise AssertionError(f"Expected exactly one scheduled activity, got {len(scheduled)}.")
    return scheduled[0]


def _resume_completed_task(generator, yielded_task: Task[object]):
    if not yielded_task.is_complete:
        raise AssertionError("Cannot resume orchestration before the yielded task completes.")
    if yielded_task.is_failed:
        return generator.throw(yielded_task.get_exception())
    return generator.send(yielded_task.get_result())


class _DelayedRunSecretStore:
    def __init__(self, run_id: str, *, missing_attempts: int) -> None:
        self._secret = build_run_secret(run_id, "ghp_race_token", lifetime=timedelta(hours=1))
        self._missing_attempts = missing_attempts
        self.get_calls = 0

    async def store(self, secret) -> None:
        self._secret = secret

    async def get(self, run_id: str):
        self.get_calls += 1
        if run_id != self._secret.run_id:
            return None
        if self.get_calls <= self._missing_attempts:
            return None
        return self._secret

    async def delete(self, run_id: str) -> None:
        return None


class _RecordingSandboxClient:
    def __init__(self) -> None:
        self.create_calls: list[dict[str, object]] = []

    def create_sandbox(self, sandbox_group: str, *, resource_group=None, **kwargs):
        call = {
            "sandbox_group": sandbox_group,
            "resource_group": resource_group,
            **kwargs,
        }
        self.create_calls.append(call)
        role = str(kwargs["labels"]["role"])
        return Sandbox(
            id=f"{role}-sandbox",
            state="Running",
            labels=dict(kwargs.get("labels") or {}),
            environment=dict(kwargs.get("environment") or {}),
            sandbox_group_id=sandbox_group,
        )


class _RecordingSandboxLifecycle:
    def __init__(self, *, fail_role: str | None = None) -> None:
        self._settings = ServiceSettings.for_local_development()
        self._fail_role = fail_role
        self.build_request_calls: list[tuple[str, str, str | None]] = []
        self.environment_calls: list[str] = []
        self.execution_calls: list[tuple[str, str, str | None]] = []
        self.cleanup_calls: list[tuple[str, bool]] = []

    def build_sandbox_request(
        self,
        role: str,
        run: SwarmRunState,
        task: SwarmTaskState | None = None,
    ) -> dict[str, object]:
        self.build_request_calls.append((role, run.id, task.id if task else None))
        return {
            "sandbox_group": "sandbox-group",
            "resource_group": "rg-test",
            "labels": {"role": role},
            "environment": {},
        }

    async def build_execution_environment(self, run: SwarmRunState) -> dict[str, str]:
        self.environment_calls.append(run.id)
        return {"GH_TOKEN": "ghp_test"}

    async def execute_planner(
        self,
        run: SwarmRunState,
        sandbox: dict[str, object] | None = None,
    ) -> PlannerExecutionResult:
        self.execution_calls.append(("planner", run.id, None))
        if self._fail_role == "planner":
            raise RuntimeError("planner boom")
        sandbox_id = _sandbox_id_value(sandbox)
        return PlannerExecutionResult(
            sandbox_id=sandbox_id,
            summary="Planner completed.",
            design_document="Plan",
            tasks=[],
        )

    async def execute_worker(
        self,
        run: SwarmRunState,
        task: SwarmTaskState,
        sandbox: dict[str, object],
    ) -> WorkerExecutionResult:
        self.execution_calls.append(("worker", run.id, task.id))
        if self._fail_role == "worker":
            raise RuntimeError("worker boom")
        return WorkerExecutionResult(
            sandbox_id=_sandbox_id_value(sandbox),
            summary="Worker completed.",
            details="Updated files.",
            branch_name=f"swarm/octo/{run.id}/{task.id}",
            round_number=1,
            head_commit_sha="1" * 40,
            parent_commit_sha="0" * 40,
            changed_files=["src/agent_swarm_service/orchestration/dts.py"],
            validation_summary="Ran 1 validation command(s); 1 succeeded and 0 failed.",
            validation_results=[],
        )

    async def execute_reviewer(
        self,
        run: SwarmRunState,
        sandbox: dict[str, object],
    ) -> ReviewerExecutionResult:
        self.execution_calls.append(("reviewer", run.id, None))
        if self._fail_role == "reviewer":
            raise RuntimeError("reviewer boom")
        return ReviewerExecutionResult(
            sandbox_id=_sandbox_id_value(sandbox),
            outcome="Approved",
            summary="Reviewer completed.",
            details="Looks good.",
            findings=[],
            fix_tasks=[],
            replan_summary=None,
            replan_findings=[],
            target_branch=f"swarm/octo/{run.id}/integration",
            pull_request_url="https://github.com/octo/repo/pull/1",
        )

    async def execute_merge(
        self,
        run: SwarmRunState,
        worker_branches,
        sandbox: dict[str, object],
    ) -> MergeExecutionResult:
        self.execution_calls.append(("merge", run.id, ",".join(item.branch_name for item in worker_branches)))
        if self._fail_role == "merge":
            raise RuntimeError("merge boom")
        return MergeExecutionResult(
            sandbox_id=_sandbox_id_value(sandbox),
            target_branch=run.target_branch or f"swarm/octo/{run.id}/integration",
            head_commit_sha="f" * 40,
            parent_commit_sha="e" * 40,
            merged_branch_names=[item.branch_name for item in worker_branches],
            changed_files=["src/agent_swarm_service/orchestration/dts.py"],
        )

    async def cleanup_sandbox(self, sandbox: dict[str, object], *, failed: bool = False) -> None:
        self.cleanup_calls.append((_sandbox_id_value(sandbox), failed))


def _dts_state(
    run: SwarmRunState,
    *,
    instance_id: str | None = None,
    runtime_status: OrchestrationStatus = OrchestrationStatus.RUNNING,
    last_updated_at: datetime | None = None,
) -> OrchestrationState:
    return OrchestrationState(
        instance_id=instance_id or run.id,
        name="swarm_orchestration",
        runtime_status=runtime_status,
        created_at=run.created_at_utc,
        last_updated_at=last_updated_at or run.last_updated_at_utc,
        serialized_input=run.model_dump_json(),
        serialized_output=None,
        serialized_custom_status=run.model_dump_json(),
        failure_details=None,
    )


def _task_failed_error(message: str) -> TaskFailedError:
    return TaskFailedError(
        message,
        pb.TaskFailureDetails(
            errorMessage=message,
            errorType="RuntimeError",
        ),
    )


def _assert_sandbox_retry_policy(test_case: unittest.TestCase, retry_policy: object) -> None:
    test_case.assertIsInstance(retry_policy, RetryPolicy)
    test_case.assertEqual(retry_policy.first_retry_interval, timedelta(seconds=5))
    test_case.assertEqual(retry_policy.max_number_of_attempts, 3)
    test_case.assertEqual(retry_policy.backoff_coefficient, 2.0)
    test_case.assertEqual(retry_policy.max_retry_interval, timedelta(seconds=30))


class _FakeAssistantMessageData:
    def __init__(self, content: str) -> None:
        self.content = content


class _FakeSessionEvent:
    def __init__(self, content: str) -> None:
        self.data = _FakeAssistantMessageData(content)


class _FakeCopilotSession:
    def __init__(self, record: dict[str, object], response_texts: list[str]) -> None:
        self._record = record
        self._response_texts = list(response_texts)

    async def __aenter__(self):
        self._record["session_entered"] = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        self._record["session_exited"] = True

    async def send_and_wait(self, prompt: str, *, timeout: float = 60.0):
        self._record.setdefault("prompts", []).append(prompt)
        self._record.setdefault("timeouts", []).append(timeout)
        self._record["prompt"] = prompt
        self._record["timeout"] = timeout
        response_text = self._response_texts.pop(0) if self._response_texts else "{}"
        return _FakeSessionEvent(response_text)

    async def get_messages(self):
        self._record["get_messages_called"] = True
        return []


class _FakeCopilotClient:
    record: dict[str, object] = {}
    response_text = "{}"
    response_texts: list[str] | None = None

    def __init__(self, config=None, *, auto_start=True, on_list_models=None) -> None:
        type(self).record["client_init"] = {
            "config": config,
            "auto_start": auto_start,
            "on_list_models": on_list_models,
        }

    async def __aenter__(self):
        type(self).record["client_entered"] = True
        return self

    async def __aexit__(self, exc_type, exc, tb) -> None:
        type(self).record["client_exited"] = True

    async def create_session(self, **kwargs):
        type(self).record["session_kwargs"] = kwargs
        response_texts = type(self).response_texts
        if response_texts is None:
            response_texts = [type(self).response_text]
        return _FakeCopilotSession(type(self).record, response_texts)


class _FakePermissionHandler:
    @staticmethod
    def approve_all(*args, **kwargs) -> None:
        return None


def _fake_copilot_sdk_modules() -> dict[str, types.ModuleType]:
    copilot_module = types.ModuleType("copilot")
    copilot_module.__path__ = []
    copilot_module.CopilotClient = _FakeCopilotClient

    generated_module = types.ModuleType("copilot.generated")
    generated_module.__path__ = []

    session_events_module = types.ModuleType("copilot.generated.session_events")
    session_events_module.AssistantMessageData = _FakeAssistantMessageData

    session_module = types.ModuleType("copilot.session")
    session_module.PermissionHandler = _FakePermissionHandler

    copilot_module.generated = generated_module
    copilot_module.session = session_module
    generated_module.session_events = session_events_module

    return {
        "copilot": copilot_module,
        "copilot.generated": generated_module,
        "copilot.generated.session_events": session_events_module,
        "copilot.session": session_module,
    }


def _make_execution_round_run(
    run_id: str,
    *,
    tasks: list[SwarmTaskState] | None = None,
) -> SwarmRunState:
    execution_tasks = list(tasks or [
        SwarmTaskState(
            id="task-1",
            title="Implement DTS-native runtime",
            status="Pending",
            summary="Push worker execution through DTS.",
            history=[SwarmTaskStatusTransition(status="Pending")],
        )
    ])
    target_branch = f"swarm/octo/{run_id}/integration"
    return SwarmRunState(
        id=run_id,
        owner=_owner(),
        title="Execution round",
        prompt="Demonstrate DTS with ACA sandboxes.",
        repository_url="https://github.com/octo/repo",
        base_branch="main",
        options=_options(),
        runtime_status="Running",
        status="Running",
        phase="Executing",
        plan=SwarmPlanState(
            design_document="Use DTS-native orchestration boundaries.",
            tasks=execution_tasks,
        ),
        tasks=execution_tasks,
        target_branch=target_branch,
        branch_state={"branch_name": target_branch},
    )


def _complete_execution_round(run: SwarmRunState, *, review_overrides: dict[str, object]) -> SwarmRunState:
    ctx = _FakeOrchestrationContext()
    generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})
    worker_batch = next(generator)
    worker_activity = _single_activity(worker_batch)
    worker_activity.complete(
        {
            "sandbox_id": "worker-1",
            "summary": "Worker finished the runtime slice.",
            "details": "Updated DTS orchestration code.",
            "branch_name": f"swarm/octo/{run.id}/task-1-r1",
            "round_number": 1,
            "head_commit_sha": "1" * 40,
            "parent_commit_sha": "0" * 40,
            "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
            "validation_summary": "Ran 1 validation command(s); 1 succeeded and 0 failed.",
            "validation_results": [
                {
                    "command": "python -m pytest tests/test_dts_runtime.py -q",
                    "exit_code": 0,
                    "status": "Succeeded",
                    "stdout": "passed",
                    "stderr": "",
                }
            ],
        }
    )
    merge_activity = _resume_completed_task(generator, worker_batch)
    if merge_activity["name"] != GIT_MERGE_ACTIVITY_NAME:
        raise AssertionError(f"Expected merge activity, got {merge_activity['name']!r}.")
    merge_activity.complete(
        {
            "sandbox_id": "merge-1",
            "target_branch": run.target_branch,
            "head_commit_sha": "1" * 40,
            "parent_commit_sha": "0" * 40,
            "merged_branch_names": [f"swarm/octo/{run.id}/task-1-r1"],
            "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
            "blocked": False,
            "blocked_reason": None,
        }
    )
    reviewer_activity = _resume_completed_task(generator, merge_activity)
    if reviewer_activity["name"] != RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME:
        raise AssertionError(f"Expected reviewer activity, got {reviewer_activity['name']!r}.")
    reviewer_result = {
        "sandbox_id": "reviewer-1",
        "outcome": "Approved",
        "summary": "Reviewer approved the execution wave.",
        "details": "Ready to publish.",
        "findings": [],
        "fix_tasks": [],
        "replan_summary": None,
        "replan_findings": [],
        "target_branch": run.target_branch,
        "pull_request_url": f"https://github.com/octo/repo/compare/main...{run.target_branch}?expand=1",
    }
    reviewer_result.update(review_overrides)
    reviewer_activity.complete(reviewer_result)
    try:
        _resume_completed_task(generator, reviewer_activity)
    except StopIteration as stop:
        return SwarmRunState.model_validate(stop.value)
    raise AssertionError("Execution round orchestration did not complete.")


class DtsRuntimeTests(unittest.TestCase):
    def test_copilot_runtime_source_avoids_sdk_signature_probing(self) -> None:
        self.assertNotIn("inspect.signature(", COPILOT_RUNTIME_SOURCE)
        self.assertNotIn("client.ask(", COPILOT_RUNTIME_SOURCE)
        self.assertNotIn("copilot_python", COPILOT_RUNTIME_SOURCE)
        self.assertIn("client.create_session(", COPILOT_RUNTIME_SOURCE)
        self.assertIn("session.send_and_wait(", COPILOT_RUNTIME_SOURCE)

    def test_copilot_runtime_force_pushes_worker_branch_head(self) -> None:
        self.assertIn(
            '_git(repo_dir, "push", "--force", "origin", f"HEAD:refs/heads/{target_branch}")',
            COPILOT_RUNTIME_SOURCE,
        )

    def test_copilot_runtime_uses_published_session_methods_directly(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        _FakeCopilotClient.record = {}
        _FakeCopilotClient.response_text = '{"summary":"planned","tasks":[{"id":"task-1"}]}'
        _FakeCopilotClient.response_texts = None
        agent = {
            "model": "gpt-5",
            "copilot_runtime": {
                "provider": "github-copilot-sdk",
                "token_environment_variable": "COPILOT_GITHUB_TOKEN",
                "api_base_url": "https://models.github.ai/inference",
            },
        }

        with patch.dict(sys.modules, _fake_copilot_sdk_modules(), clear=False), patch.dict(
            os.environ,
            {"COPILOT_GITHUB_TOKEN": "ghp_example_token"},
            clear=False,
        ):
            payload = copilot_runtime._invoke_copilot(
                agent,
                system_prompt="Return JSON only.",
                prompt="Plan the task.",
                working_directory=REPO_ROOT,
            )

        self.assertEqual(payload["summary"], "planned")
        self.assertEqual(payload["tasks"][0]["id"], "task-1")
        self.assertEqual(_FakeCopilotClient.record["prompt"], "Plan the task.")
        self.assertEqual(_FakeCopilotClient.record["timeout"], 300.0)
        self.assertEqual(
            _FakeCopilotClient.record["session_kwargs"],
            {
                "on_permission_request": _FakePermissionHandler.approve_all,
                "github_token": "ghp_example_token",
                "model": "gpt-5",
                "system_message": {"mode": "append", "content": "Return JSON only."},
                "available_tools": [],
                "working_directory": str(REPO_ROOT),
            },
        )

    def test_copilot_runtime_omits_tool_restrictions_when_tools_are_enabled(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        _FakeCopilotClient.record = {}
        _FakeCopilotClient.response_text = '{"summary":"planned","tasks":[{"id":"task-1"}]}'
        _FakeCopilotClient.response_texts = None
        agent = {
            "model": "gpt-5",
            "copilot_runtime": {
                "provider": "github-copilot-sdk",
                "token_environment_variable": "COPILOT_GITHUB_TOKEN",
            },
        }

        with patch.dict(sys.modules, _fake_copilot_sdk_modules(), clear=False), patch.dict(
            os.environ,
            {"COPILOT_GITHUB_TOKEN": "ghp_example_token"},
            clear=False,
        ):
            payload = copilot_runtime._invoke_copilot(
                agent,
                system_prompt="Return JSON only.",
                prompt="Implement the worker task.",
                working_directory=REPO_ROOT,
                allow_tools=True,
            )

        self.assertEqual(payload["summary"], "planned")
        self.assertNotIn("available_tools", _FakeCopilotClient.record["session_kwargs"])

    def test_planner_runtime_prompt_requires_explicit_task_guidance(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        captured: dict[str, object] = {}
        request = {
            "sandboxId": "planner-1",
            "runId": "run-planner-guidance",
            "prompt": "Plan a bounded docs task.",
            "repository": {
                "host": "github.com",
                "owner": "octo",
                "name": "repo",
                "base_branch": "main",
            },
            "feedback": None,
            "pendingReplanSummary": None,
            "pendingReplanFindings": [],
            "agent": {
                "model": "gpt-5",
                "copilot_runtime": {
                    "provider": "github-copilot-sdk",
                    "token_environment_variable": "COPILOT_GITHUB_TOKEN",
                },
            },
        }

        def fake_invoke(agent, *, system_prompt, prompt, working_directory, allow_tools=False):
            del agent, system_prompt, working_directory, allow_tools
            captured["prompt"] = prompt
            return {
                "summary": "Planner created a bounded task.",
                "design_document": "Use explicit task metadata only.",
                "tasks": [
                    {
                        "id": "task-1",
                        "title": "Refresh README guidance",
                        "summary": "Clarify the quickstart prerequisites.",
                        "target_files": ["README.md"],
                        "acceptance_criteria": ["README.md explains the quickstart prerequisites."],
                        "validation_commands": [],
                    }
                ],
            }

        with _worker_test_repo("swarm/octo/run-planner-guidance/integration") as (repo_dir, _), patch.object(
            copilot_runtime, "_invoke_copilot", new=fake_invoke
        ):
            result = copilot_runtime._run_planner(request, repo_dir)

        self.assertEqual(result["tasks"][0]["target_files"], ["README.md"])
        self.assertEqual(
            result["tasks"][0]["acceptance_criteria"],
            ["README.md explains the quickstart prerequisites."],
        )
        self.assertIn('"target_files": ["string"]', str(captured["prompt"]))
        self.assertIn('"acceptance_criteria": ["string"]', str(captured["prompt"]))
        self.assertIn("Do not rely on downstream inference", str(captured["prompt"]))

    def test_reviewer_runtime_prompt_requires_explicit_fix_task_guidance(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        captured: dict[str, object] = {}

        def fake_invoke(agent, *, system_prompt, prompt, working_directory, allow_tools=False):
            del agent, system_prompt, working_directory, allow_tools
            captured["prompt"] = prompt
            return {
                "outcome": "approved",
                "summary": "Reviewer approved the change.",
                "details": "No follow-up work is required.",
                "findings": [],
                "fix_tasks": [],
                "replan_summary": None,
                "replan_findings": [],
            }

        with _worker_test_repo("swarm/octo/run-reviewer-guidance/integration") as (repo_dir, _), patch.object(
            copilot_runtime, "_invoke_copilot", new=fake_invoke
        ):
            head_sha = _git_command(repo_dir, "rev-parse", "HEAD").stdout.strip()
            request = {
                "sandboxId": "reviewer-1",
                "runId": "run-reviewer-guidance",
                "prompt": "Review the bounded docs task.",
                "repository": {
                    "host": "github.com",
                    "owner": "octo",
                    "name": "repo",
                    "base_branch": "main",
                },
                "branchState": {
                    "current_head_sha": head_sha,
                    "reviewed_head_sha": "",
                    "approved_head_sha": "",
                    "current_wave_round": 0,
                    "active_wave": 1,
                },
                "completedTasks": [],
                "agent": {
                    "model": "gpt-5",
                    "copilot_runtime": {
                        "provider": "github-copilot-sdk",
                        "token_environment_variable": "COPILOT_GITHUB_TOKEN",
                    },
                },
                "targetBranch": "swarm/octo/run-reviewer-guidance/integration",
                "pullRequestUrl": None,
            }
            result = copilot_runtime._run_reviewer(request, repo_dir)

        self.assertEqual(result["outcome"], "Approved")
        self.assertIn('"target_files": ["string"]', str(captured["prompt"]))
        self.assertIn('"acceptance_criteria": ["string"]', str(captured["prompt"]))
        self.assertIn("Do not rely on downstream inference", str(captured["prompt"]))

    def test_copilot_runtime_extracts_wrapped_single_json_object(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        _FakeCopilotClient.record = {}
        _FakeCopilotClient.response_text = (
            "Sure — here is the worker payload.\n"
            '{"summary":"planned","tasks":[{"id":"task-1"}]}\n'
            "Let me know if you need anything else."
        )
        _FakeCopilotClient.response_texts = None
        agent = {
            "model": "gpt-5",
            "copilot_runtime": {
                "provider": "github-copilot-sdk",
                "token_environment_variable": "COPILOT_GITHUB_TOKEN",
            },
        }

        with patch.dict(sys.modules, _fake_copilot_sdk_modules(), clear=False), patch.dict(
            os.environ,
            {"COPILOT_GITHUB_TOKEN": "ghp_example_token"},
            clear=False,
        ):
            payload = copilot_runtime._invoke_copilot(
                agent,
                system_prompt="Return JSON only.",
                prompt="Plan the task.",
                working_directory=REPO_ROOT,
            )

        self.assertEqual(payload["summary"], "planned")
        self.assertEqual(payload["tasks"][0]["id"], "task-1")
        self.assertEqual(_FakeCopilotClient.record["prompts"], ["Plan the task."])

    def test_copilot_runtime_retries_worker_payload_when_first_reply_is_invalid_json(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        _FakeCopilotClient.record = {}
        _FakeCopilotClient.response_texts = [
            (
                '{"summary":"done","details":"updated worker\nwith a newline","commit_message":"worker change"}'
            ),
            (
                '{"summary":"done","details":"updated worker\\nwith a newline","commit_message":"worker change"}'
            ),
        ]
        agent = {
            "model": "gpt-5",
            "copilot_runtime": {
                "provider": "github-copilot-sdk",
                "token_environment_variable": "COPILOT_GITHUB_TOKEN",
            },
        }

        with patch.dict(sys.modules, _fake_copilot_sdk_modules(), clear=False), patch.dict(
            os.environ,
            {"COPILOT_GITHUB_TOKEN": "ghp_example_token"},
            clear=False,
        ):
            payload = copilot_runtime._invoke_copilot(
                agent,
                system_prompt="Return JSON only.",
                prompt="Implement the worker task.",
                working_directory=REPO_ROOT,
            )

        self.assertEqual(payload["commit_message"], "worker change")
        self.assertEqual(payload["details"], "updated worker\nwith a newline")
        self.assertEqual(len(_FakeCopilotClient.record["prompts"]), 2)
        self.assertEqual(_FakeCopilotClient.record["prompts"][0], "Implement the worker task.")
        self.assertIn("Reply again with exactly one valid JSON object", _FakeCopilotClient.record["prompts"][1])
        self.assertIn("escape newlines, quotes, and backslashes", _FakeCopilotClient.record["prompts"][1])

    def test_worker_runtime_commits_direct_repo_edits_without_file_operations(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        target_branch = "swarm/octo/run-worker-direct-edit/integration"
        with _worker_test_repo(target_branch) as (repo_dir, remote_dir):
            request = _worker_request(repo_dir, target_branch, validation_commands=["git rev-parse HEAD"])
            captured: dict[str, object] = {}

            def fake_invoke(agent, *, system_prompt, prompt, working_directory, allow_tools=False):
                del agent, system_prompt
                captured["prompt"] = prompt
                captured["allow_tools"] = allow_tools
                source_path = working_directory / "src" / "example.py"
                source_path.parent.mkdir(parents=True, exist_ok=True)
                source_path.write_text("print('worker direct edit')\n", encoding="utf-8")
                return {
                    "summary": "Worker updated the repository directly.",
                    "details": "Edited src/example.py in the sandbox worktree.",
                    "commit_message": "Worker direct edit",
                }

            with patch.object(copilot_runtime, "_invoke_copilot", new=fake_invoke):
                result = copilot_runtime._run_worker(request, repo_dir)

            self.assertTrue(captured["allow_tools"])
            self.assertIn("Apply edits directly to the repository working tree", str(captured["prompt"]))
            self.assertNotIn('"file_operations":', str(captured["prompt"]))
            self.assertFalse(result["no_changes"])
            self.assertEqual(result["changed_files"], ["src/example.py"])
            self.assertEqual(result["validation_results"][0]["command"], "git rev-parse HEAD")
            self.assertNotEqual(result["head_commit_sha"], result["parent_commit_sha"])
            remote_head = _git_command(remote_dir, "rev-parse", f"refs/heads/{target_branch}").stdout.strip()
            self.assertEqual(remote_head, result["head_commit_sha"])

    def test_worker_runtime_requires_explicit_no_change_flag_for_clean_worktree(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        target_branch = "swarm/octo/run-worker-clean/integration"
        with _worker_test_repo(target_branch) as (repo_dir, _):
            request = _worker_request(repo_dir, target_branch)

            def fake_invoke(agent, *, system_prompt, prompt, working_directory, allow_tools=False):
                del agent, system_prompt, prompt, working_directory, allow_tools
                return {
                    "summary": "Nothing changed.",
                    "details": "The task was already satisfied.",
                }

            with patch.object(copilot_runtime, "_invoke_copilot", new=fake_invoke):
                with self.assertRaisesRegex(
                    copilot_runtime.RuntimeContractError,
                    "did not produce repository changes and did not declare a no-change outcome",
                ):
                    copilot_runtime._run_worker(request, repo_dir)

    def test_worker_runtime_allows_explicit_no_change_outcome(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        target_branch = "swarm/octo/run-worker-no-change/integration"
        with _worker_test_repo(target_branch) as (repo_dir, remote_dir):
            request = _worker_request(repo_dir, target_branch, validation_commands=["git rev-parse HEAD"])

            def fake_invoke(agent, *, system_prompt, prompt, working_directory, allow_tools=False):
                del agent, system_prompt, prompt, working_directory
                self.assertTrue(allow_tools)
                return {
                    "summary": "Task already satisfied.",
                    "details": "Verified the existing repository state already matches the request.",
                    "no_changes": True,
                }

            with patch.object(copilot_runtime, "_invoke_copilot", new=fake_invoke):
                result = copilot_runtime._run_worker(request, repo_dir)

            self.assertTrue(result["no_changes"])
            self.assertEqual(result["changed_files"], [])
            self.assertEqual(result["head_commit_sha"], result["parent_commit_sha"])
            remote_head = _git_command(remote_dir, "rev-parse", f"refs/heads/{target_branch}").stdout.strip()
            self.assertEqual(remote_head, result["head_commit_sha"])

    def test_merge_runtime_deletes_remote_worker_branches_after_successful_integration_push(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        target_branch = "swarm/octo/run-merge-delete/integration"
        worker_branch_1 = "swarm/octo/run-merge-delete/task-1-r1"
        worker_branch_2 = "swarm/octo/run-merge-delete/task-2-r1"
        with _worker_test_repo(target_branch) as (repo_dir, remote_dir):
            target_head = _git_command(repo_dir, "rev-parse", "HEAD").stdout.strip()
            _git_command(repo_dir, "push", "-u", "origin", target_branch)

            _git_command(repo_dir, "checkout", "-B", worker_branch_1, target_head)
            (repo_dir / "src").mkdir(parents=True, exist_ok=True)
            (repo_dir / "src" / "feature.py").write_text("print('feature 1')\n", encoding="utf-8")
            _git_command(repo_dir, "add", "src/feature.py")
            _git_command(repo_dir, "commit", "-m", "Worker task 1")
            worker_head_1 = _git_command(repo_dir, "rev-parse", "HEAD").stdout.strip()
            _git_command(repo_dir, "push", "-u", "origin", worker_branch_1)

            _git_command(repo_dir, "checkout", "-B", worker_branch_2, target_head)
            (repo_dir / "docs").mkdir(parents=True, exist_ok=True)
            (repo_dir / "docs" / "quickstart.md").write_text("quickstart\n", encoding="utf-8")
            _git_command(repo_dir, "add", "docs/quickstart.md")
            _git_command(repo_dir, "commit", "-m", "Worker task 2")
            worker_head_2 = _git_command(repo_dir, "rev-parse", "HEAD").stdout.strip()
            _git_command(repo_dir, "push", "-u", "origin", worker_branch_2)

            _git_command(repo_dir, "checkout", "-B", target_branch, target_head)
            result = copilot_runtime._run_merge(
                _merge_request(
                    repo_dir,
                    target_branch,
                    [
                        {
                            "branch_name": worker_branch_1,
                            "head_commit_sha": worker_head_1,
                            "parent_commit_sha": target_head,
                            "changed_files": ["src/feature.py"],
                        },
                        {
                            "branch_name": worker_branch_2,
                            "head_commit_sha": worker_head_2,
                            "parent_commit_sha": target_head,
                            "changed_files": ["docs/quickstart.md"],
                        },
                    ],
                ),
                repo_dir,
            )

            self.assertEqual(result["merged_branch_names"], [worker_branch_1, worker_branch_2])
            self.assertEqual(result["deleted_branch_names"], [worker_branch_1, worker_branch_2])
            self.assertTrue(_remote_branch_exists(remote_dir, target_branch))
            self.assertFalse(_remote_branch_exists(remote_dir, worker_branch_1))
            self.assertFalse(_remote_branch_exists(remote_dir, worker_branch_2))
            remote_head = _git_command(remote_dir, "rev-parse", f"refs/heads/{target_branch}").stdout.strip()
            self.assertEqual(remote_head, result["head_commit_sha"])

    def test_merge_runtime_never_deletes_integration_branch_when_it_is_listed_as_merged(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        target_branch = "swarm/octo/run-merge-skip-target/integration"
        with _worker_test_repo(target_branch) as (repo_dir, remote_dir):
            target_head = _git_command(repo_dir, "rev-parse", "HEAD").stdout.strip()
            _git_command(repo_dir, "push", "-u", "origin", target_branch)
            _git_command(repo_dir, "checkout", "-B", "target-update-source", target_head)
            (repo_dir / "README.md").write_text("integration update\n", encoding="utf-8")
            _git_command(repo_dir, "add", "README.md")
            _git_command(repo_dir, "commit", "-m", "Integration update")
            updated_head = _git_command(repo_dir, "rev-parse", "HEAD").stdout.strip()
            _git_command(repo_dir, "checkout", "-B", target_branch, target_head)

            result = copilot_runtime._run_merge(
                _merge_request(
                    repo_dir,
                    target_branch,
                    [
                        {
                            "branch_name": target_branch,
                            "head_commit_sha": updated_head,
                            "parent_commit_sha": target_head,
                            "changed_files": ["README.md"],
                        }
                    ],
                ),
                repo_dir,
            )

            self.assertEqual(result["merged_branch_names"], [target_branch])
            self.assertEqual(result["deleted_branch_names"], [])
            self.assertTrue(_remote_branch_exists(remote_dir, target_branch))
            remote_head = _git_command(remote_dir, "rev-parse", f"refs/heads/{target_branch}").stdout.strip()
            self.assertEqual(remote_head, updated_head)

    def test_merge_runtime_does_not_delete_worker_branches_when_fan_in_is_blocked(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        target_branch = "swarm/octo/run-merge-blocked-delete/integration"
        worker_branch = "swarm/octo/run-merge-blocked-delete/task-1-r1"
        with _worker_test_repo(target_branch) as (repo_dir, remote_dir):
            target_head = _git_command(repo_dir, "rev-parse", "HEAD").stdout.strip()
            _git_command(repo_dir, "push", "-u", "origin", target_branch)
            _git_command(repo_dir, "checkout", "-B", worker_branch, target_head)
            (repo_dir / "README.md").write_text("worker update\n", encoding="utf-8")
            _git_command(repo_dir, "add", "README.md")
            _git_command(repo_dir, "commit", "-m", "Worker conflicting update")
            worker_head = _git_command(repo_dir, "rev-parse", "HEAD").stdout.strip()
            _git_command(repo_dir, "push", "-u", "origin", worker_branch)
            _git_command(repo_dir, "checkout", "-B", target_branch, target_head)
            (repo_dir / "README.md").write_text("integration update\n", encoding="utf-8")
            _git_command(repo_dir, "add", "README.md")
            _git_command(repo_dir, "commit", "-m", "Integration conflicting update")

            with patch.object(copilot_runtime, "_resolve_merge_conflict", return_value="conflict remained"):
                result = copilot_runtime._run_merge(
                    _merge_request(
                        repo_dir,
                        target_branch,
                        [
                            {
                                "branch_name": worker_branch,
                                "head_commit_sha": worker_head,
                                "parent_commit_sha": target_head,
                                "changed_files": ["README.md"],
                            }
                        ],
                    ),
                    repo_dir,
                )

            self.assertTrue(result["blocked"])
            self.assertEqual(result["deleted_branch_names"], [])
            self.assertTrue(_remote_branch_exists(remote_dir, worker_branch))
            remote_head = _git_command(remote_dir, "rev-parse", f"refs/heads/{target_branch}").stdout.strip()
            self.assertEqual(remote_head, target_head)

    def test_copilot_runtime_surfaces_retry_failure_preview(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        _FakeCopilotClient.record = {}
        _FakeCopilotClient.response_texts = ["not json", "still not json"]
        agent = {
            "model": "gpt-5",
            "copilot_runtime": {
                "provider": "github-copilot-sdk",
                "token_environment_variable": "COPILOT_GITHUB_TOKEN",
            },
        }

        with patch.dict(sys.modules, _fake_copilot_sdk_modules(), clear=False), patch.dict(
            os.environ,
            {"COPILOT_GITHUB_TOKEN": "ghp_example_token"},
            clear=False,
        ):
            with self.assertRaisesRegex(
                copilot_runtime.RuntimeContractError,
                "Initial parse error: Copilot SDK response was not valid JSON",
            ) as error:
                copilot_runtime._invoke_copilot(
                    agent,
                    system_prompt="Return JSON only.",
                    prompt="Implement the worker task.",
                    working_directory=REPO_ROOT,
                )

        self.assertIn("Last response preview: still not json", str(error.exception))

    def test_reviewer_prompt_enforces_exact_outcome_tokens_and_examples(self) -> None:
        self.assertIn(
            'The outcome field must be exactly one of "approved", "fixTasks", or "replan" and nothing else.',
            COPILOT_RUNTIME_SOURCE,
        )
        self.assertIn(
            'Never return "Approved", "FixTasks", "Replan", "Rejected", or any other synonym.',
            COPILOT_RUNTIME_SOURCE,
        )
        self.assertIn('1. Approved review', COPILOT_RUNTIME_SOURCE)
        self.assertIn('"outcome": "approved"', COPILOT_RUNTIME_SOURCE)
        self.assertIn('"outcome": "fixTasks"', COPILOT_RUNTIME_SOURCE)
        self.assertIn('"outcome": "replan"', COPILOT_RUNTIME_SOURCE)

    def test_reviewer_runtime_accepts_only_exact_prompt_outcome_tokens(self) -> None:
        from agent_swarm_service.orchestration import copilot_runtime

        self.assertEqual(copilot_runtime._canonicalize_reviewer_outcome("approved"), "Approved")
        self.assertEqual(copilot_runtime._canonicalize_reviewer_outcome("fixTasks"), "FixTasks")
        self.assertEqual(copilot_runtime._canonicalize_reviewer_outcome("replan"), "Replan")
        with self.assertRaisesRegex(
            copilot_runtime.RuntimeContractError,
            'Reviewer runtime must return outcome exactly one of "approved", "fixTasks", or "replan".',
        ):
            copilot_runtime._canonicalize_reviewer_outcome("Rejected")

    def test_build_worker_registration_exposes_only_public_named_sandbox_activities(self) -> None:
        registration = build_worker_registration()

        self.assertEqual(
            registration.activities,
            (
                RUN_PLANNER_IN_SANDBOX_ACTIVITY_NAME,
                RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME,
                RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME,
                GIT_MERGE_ACTIVITY_NAME,
                PUBLISH_TO_GITHUB_ACTIVITY_NAME,
            ),
        )

    def test_run_planner_activity_can_own_sandbox_lifecycle_under_existing_name(self) -> None:
        run = _make_execution_round_run("run-planner-named")
        sandbox_client = _RecordingSandboxClient()
        sandbox_lifecycle = _RecordingSandboxLifecycle()
        activity = _make_run_planner_activity(
            sandbox_lifecycle=sandbox_lifecycle,
            sandbox_client=sandbox_client,
        )

        result = activity(None, {"run": run.model_dump(mode="json")})

        self.assertEqual(activity.__name__, RUN_PLANNER_IN_SANDBOX_ACTIVITY_NAME)
        self.assertEqual(result["sandbox_id"], "planner-sandbox")
        self.assertEqual(sandbox_lifecycle.build_request_calls, [("planner", run.id, None)])
        self.assertEqual(sandbox_lifecycle.environment_calls, [run.id])
        self.assertEqual(sandbox_lifecycle.execution_calls, [("planner", run.id, None)])
        self.assertEqual(sandbox_lifecycle.cleanup_calls, [("planner-sandbox", False)])
        self.assertEqual(sandbox_client.create_calls[0]["environment"]["GH_TOKEN"], "ghp_test")

    def test_run_worker_activity_can_own_sandbox_lifecycle_under_existing_name(self) -> None:
        run = _make_execution_round_run("run-worker-named")
        sandbox_client = _RecordingSandboxClient()
        sandbox_lifecycle = _RecordingSandboxLifecycle()
        activity = _make_run_worker_activity(
            sandbox_lifecycle=sandbox_lifecycle,
            sandbox_client=sandbox_client,
        )

        result = activity(
            None,
            {
                "run": run.model_dump(mode="json"),
                "task": run.tasks[0].model_dump(mode="json"),
            },
        )

        self.assertEqual(activity.__name__, RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME)
        self.assertEqual(result["sandbox_id"], "worker-sandbox")
        self.assertEqual(sandbox_lifecycle.build_request_calls, [("worker", run.id, run.tasks[0].id)])
        self.assertEqual(sandbox_lifecycle.environment_calls, [run.id])
        self.assertEqual(sandbox_lifecycle.execution_calls, [("worker", run.id, run.tasks[0].id)])
        self.assertEqual(sandbox_lifecycle.cleanup_calls, [("worker-sandbox", False)])
        self.assertEqual(sandbox_client.create_calls[0]["environment"]["GH_TOKEN"], "ghp_test")

    def test_run_worker_activity_logs_combined_activity_lifecycle(self) -> None:
        run = _make_execution_round_run("run-worker-logging")
        sandbox_client = _RecordingSandboxClient()
        sandbox_lifecycle = _RecordingSandboxLifecycle()
        activity = _make_run_worker_activity(
            sandbox_lifecycle=sandbox_lifecycle,
            sandbox_client=sandbox_client,
        )

        with self.assertLogs("agent_swarm_service.orchestration.dts", level="INFO") as logs:
            result = activity(
                None,
                {
                    "run": run.model_dump(mode="json"),
                    "task": run.tasks[0].model_dump(mode="json"),
                },
            )

        joined = "\n".join(logs.output)
        self.assertEqual(result["sandbox_id"], "worker-sandbox")
        self.assertIn("Starting sandbox role activity", joined)
        self.assertIn("Created sandbox for role activity", joined)
        self.assertIn("Completed sandbox role activity", joined)
        self.assertIn('"activity": "RunWorkerInSandboxActivity"', joined)
        self.assertIn('"role": "worker"', joined)
        self.assertIn('"run_id": "run-worker-logging"', joined)
        self.assertIn(f'"task_id": "{run.tasks[0].id}"', joined)
        self.assertIn('"sandbox_id": "worker-sandbox"', joined)

    def test_run_worker_activity_marks_failed_cleanup_on_role_error(self) -> None:
        run = _make_execution_round_run("run-worker-fail")
        sandbox_client = _RecordingSandboxClient()
        sandbox_lifecycle = _RecordingSandboxLifecycle(fail_role="worker")
        activity = _make_run_worker_activity(
            sandbox_lifecycle=sandbox_lifecycle,
            sandbox_client=sandbox_client,
        )

        with self.assertRaisesRegex(RuntimeError, "worker boom"):
            activity(
                None,
                {
                    "run": run.model_dump(mode="json"),
                    "task": run.tasks[0].model_dump(mode="json"),
                },
            )

        self.assertEqual(sandbox_lifecycle.cleanup_calls, [("worker-sandbox", True)])
        self.assertEqual(len(sandbox_client.create_calls), 1)

    def test_run_worker_activity_rejects_missing_task_before_creating_sandbox(self) -> None:
        run = _make_execution_round_run("run-worker-missing-task")
        sandbox_client = _RecordingSandboxClient()
        sandbox_lifecycle = _RecordingSandboxLifecycle()
        activity = _make_run_worker_activity(
            sandbox_lifecycle=sandbox_lifecycle,
            sandbox_client=sandbox_client,
        )

        with self.assertRaisesRegex(ValueError, "Worker activity requires a task payload."):
            activity(None, {"run": run.model_dump(mode="json")})

        self.assertEqual(sandbox_client.create_calls, [])
        self.assertEqual(sandbox_lifecycle.cleanup_calls, [])

    def test_run_review_activity_can_own_sandbox_lifecycle_under_existing_name(self) -> None:
        run = _make_execution_round_run("run-review-named")
        sandbox_client = _RecordingSandboxClient()
        sandbox_lifecycle = _RecordingSandboxLifecycle()
        activity = _make_run_review_activity(
            sandbox_lifecycle=sandbox_lifecycle,
            sandbox_client=sandbox_client,
        )

        result = activity(None, {"run": run.model_dump(mode="json")})

        self.assertEqual(activity.__name__, RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)
        self.assertEqual(result["sandbox_id"], "reviewer-sandbox")
        self.assertEqual(sandbox_lifecycle.build_request_calls, [("reviewer", run.id, None)])
        self.assertEqual(sandbox_lifecycle.environment_calls, [run.id])
        self.assertEqual(sandbox_lifecycle.execution_calls, [("reviewer", run.id, None)])
        self.assertEqual(sandbox_lifecycle.cleanup_calls, [("reviewer-sandbox", False)])
        self.assertEqual(sandbox_client.create_calls[0]["environment"]["GH_TOKEN"], "ghp_test")

    def test_git_merge_activity_can_own_sandbox_lifecycle_under_existing_name(self) -> None:
        run = _make_execution_round_run("run-merge-named")
        sandbox_client = _RecordingSandboxClient()
        sandbox_lifecycle = _RecordingSandboxLifecycle()
        activity = _make_git_merge_activity(
            sandbox_lifecycle=sandbox_lifecycle,
            sandbox_client=sandbox_client,
        )

        result = activity(
            None,
            {
                "run": run.model_dump(mode="json"),
                "worker_branches": [
                    {
                        "task_id": run.tasks[0].id,
                        "branch_name": f"swarm/octo/{run.id}/task-1-r1",
                        "head_commit_sha": "1" * 40,
                        "parent_commit_sha": "0" * 40,
                        "round_number": 1,
                        "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                    }
                ],
            },
        )

        self.assertEqual(activity.__name__, GIT_MERGE_ACTIVITY_NAME)
        self.assertEqual(result["sandbox_id"], "merge-sandbox")
        self.assertEqual(sandbox_lifecycle.build_request_calls, [("merge", run.id, None)])
        self.assertEqual(sandbox_lifecycle.environment_calls, [run.id])
        self.assertEqual(
            sandbox_lifecycle.execution_calls,
            [("merge", run.id, f"swarm/octo/{run.id}/task-1-r1")],
        )
        self.assertEqual(sandbox_lifecycle.cleanup_calls, [("merge-sandbox", False)])
        self.assertEqual(sandbox_client.create_calls[0]["environment"]["GH_TOKEN"], "ghp_test")

    def test_connection_string_parser_reads_endpoint_and_taskhub(self) -> None:
        info = DtsConnectionInfo.from_connection_string(
            "Endpoint=https://example.eastus.durabletask.io;Authentication=ManagedIdentity;TaskHub=swarm;ClientId=abc123"
        )

        self.assertEqual(info.host_address, "https://example.eastus.durabletask.io")
        self.assertEqual(info.taskhub, "swarm")
        self.assertTrue(info.secure_channel)
        self.assertEqual(info.client_id, "abc123")

    def test_coordinator_create_run_schedules_dts_and_indexes_ownership(self) -> None:
        backend = InMemoryRuntimeStorageBackend()
        client = _FakeDtsClient()
        coordinator = DtsSwarmCoordinator(client, DurableRunOwnershipStore(backend))

        run = _run(
            coordinator.create_run(
                owner=_owner(),
                prompt="Switch the swarm runtime to DTS",
                repository_url="https://github.com/octo/repo",
                base_branch="main",
                options=_options(),
            )
        )

        self.assertEqual(run.owner.user_id, "session-101")
        self.assertEqual(client.scheduled[0][2], run.id)
        self.assertEqual(client.scheduled[0][3]["ownerSessionId"], "session-101")
        self.assertEqual(run.target_branch, f"swarm/octo/{run.id[:8]}/integration")
        self.assertEqual(run.branch_state.branch_name, run.target_branch)
        self.assertEqual(run.branch_state.current_head.reference_type, "Branch")
        self.assertEqual(run.branch_state.approved_head.reference_type, "Unavailable")
        self.assertIsNone(run.branch_state.approved_branch_name)
        self.assertEqual(run.branch_state.merge_state.status, "Queued")
        stored = _run(backend.read_json(f"run-ownership/session-101/{run.id}.json"))
        self.assertEqual(stored["run_id"], run.id)

    def test_get_run_prefers_custom_status_and_overlays_dts_runtime_status(self) -> None:
        backend = InMemoryRuntimeStorageBackend()
        client = _FakeDtsClient()
        coordinator = DtsSwarmCoordinator(client, DurableRunOwnershipStore(backend))
        now = datetime.now(UTC)
        run = SwarmRunState(
            id="run-123",
            owner=_owner(),
            title="DTS status overlay",
            prompt="Use DTS as the run state store",
            repository_url="https://github.com/octo/repo",
            options=_options(),
            runtime_status="Running",
            status="Running",
            phase="Reviewing",
            message="Reviewer is running.",
            created_at_utc=now - timedelta(minutes=5),
            last_updated_at_utc=now - timedelta(minutes=1),
        )
        client.states[run.id] = OrchestrationState(
            instance_id=run.id,
            name="swarm_orchestration",
            runtime_status=OrchestrationStatus.SUSPENDED,
            created_at=run.created_at_utc,
            last_updated_at=now,
            serialized_input=run.model_dump_json(),
            serialized_output=None,
            serialized_custom_status=run.model_dump_json(),
            failure_details=None,
        )

        loaded = _run(coordinator.get_run(run.id))

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.runtime_status, "Suspended")
        self.assertEqual(loaded.status, "Suspended")
        self.assertEqual(loaded.phase, "Suspended")
        self.assertGreaterEqual(loaded.last_updated_at_utc, now)

    def test_get_run_normalizes_naive_dts_timestamps(self) -> None:
        backend = InMemoryRuntimeStorageBackend()
        client = _FakeDtsClient()
        coordinator = DtsSwarmCoordinator(client, DurableRunOwnershipStore(backend))
        now = datetime.now(UTC)
        run = SwarmRunState(
            id="run-naive",
            owner=_owner(),
            title="Naive DTS timestamp",
            prompt="Handle naive timestamps from DTS.",
            repository_url="https://github.com/octo/repo",
            options=_options(),
            created_at_utc=now - timedelta(minutes=2),
            last_updated_at_utc=now - timedelta(minutes=1),
        )
        client.states[run.id] = OrchestrationState(
            instance_id=run.id,
            name="swarm_orchestration",
            runtime_status=OrchestrationStatus.FAILED,
            created_at=run.created_at_utc.replace(tzinfo=None),
            last_updated_at=now.replace(tzinfo=None),
            serialized_input=run.model_dump_json(),
            serialized_output=None,
            serialized_custom_status=run.model_dump_json(),
            failure_details=None,
        )

        loaded = _run(coordinator.get_run(run.id))

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.runtime_status, "Failed")
        self.assertEqual(loaded.last_updated_at_utc.tzinfo, UTC)
        self.assertGreaterEqual(loaded.last_updated_at_utc, now)

    def test_get_run_prefers_active_execution_round_head_over_stale_root_checkpoint(self) -> None:
        backend = InMemoryRuntimeStorageBackend()
        client = _FakeDtsClient()
        coordinator = DtsSwarmCoordinator(client, DurableRunOwnershipStore(backend))
        now = datetime.now(UTC)
        root = _make_execution_round_run("run-head-state").model_copy(
            update={
                "message": "Root checkpoint is still waiting on the execution round.",
                "last_updated_at_utc": now - timedelta(minutes=2),
                "checkpoint": CoordinatorCheckpoint(
                    run_id="run-head-state",
                    phase="Executing",
                    status="Running",
                    sequence=2,
                ),
            }
        )
        active_execution = root.model_copy(
            update={
                "message": "Execution round advanced the long-lived run head branch.",
                "target_branch": "swarm/octo/run-head-state/integration",
                "last_updated_at_utc": now,
                "checkpoint": CoordinatorCheckpoint(
                    run_id="run-head-state",
                    phase="Reviewing",
                    status="Running",
                    sequence=4,
                ),
            }
        )
        client.states[root.id] = _dts_state(root, last_updated_at=root.last_updated_at_utc)
        client.states[execution_round_instance_id(_owner().user_id, root.id, 1)] = _dts_state(
            active_execution,
            instance_id=execution_round_instance_id(_owner().user_id, root.id, 1),
            last_updated_at=active_execution.last_updated_at_utc,
        )

        loaded = _run(coordinator.get_run(root.id))

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.target_branch, "swarm/octo/run-head-state/integration")
        self.assertEqual(loaded.message, "Execution round advanced the long-lived run head branch.")
        self.assertEqual(loaded.checkpoint.sequence, 4)

    def test_get_run_prefers_failed_sub_orchestration_state_over_stale_failed_root_snapshot(self) -> None:
        backend = InMemoryRuntimeStorageBackend()
        client = _FakeDtsClient()
        coordinator = DtsSwarmCoordinator(client, DurableRunOwnershipStore(backend))
        now = datetime.now(UTC)
        root = SwarmRunState(
            id="run-planning-failure",
            owner=_owner(),
            title="Planner failed",
            prompt="Surface the failed planning details.",
            repository_url="https://github.com/octo/repo",
            base_branch="main",
            options=_options(),
            runtime_status="Running",
            status="Running",
            phase="Planning",
            message="Planner is starting.",
            last_updated_at_utc=now - timedelta(minutes=5),
            checkpoint=CoordinatorCheckpoint(
                run_id="run-planning-failure",
                phase="Planning",
                status="Running",
                sequence=1,
            ),
        )
        failed_planning = root.model_copy(
            update={
                "runtime_status": "Failed",
                "status": "Failed",
                "phase": "Failed",
                "message": "Planner sandbox failed.",
                "failure_message": "Planner sandbox failed with exit code 1: traceback",
                "last_updated_at_utc": now - timedelta(seconds=1),
                "planner_summaries": [
                    SwarmActivitySummary(
                        id="planner-1",
                        kind="planner",
                        title="Create execution plan",
                        status="failed",
                        summary="Planner sandbox failed.",
                        details="Planner sandbox failed with exit code 1: traceback",
                    )
                ],
                "checkpoint": CoordinatorCheckpoint(
                    run_id="run-planning-failure",
                    phase="Failed",
                    status="Failed",
                    sequence=2,
                ),
            }
        )
        client.states[root.id] = _dts_state(
            root,
            runtime_status=OrchestrationStatus.FAILED,
            last_updated_at=now,
        )
        client.states[planning_instance_id(_owner().user_id, root.id)] = _dts_state(
            failed_planning,
            instance_id=planning_instance_id(_owner().user_id, root.id),
            runtime_status=OrchestrationStatus.FAILED,
            last_updated_at=failed_planning.last_updated_at_utc,
        )

        loaded = _run(coordinator.get_run(root.id))

        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.runtime_status, "Failed")
        self.assertEqual(loaded.failure_message, "Planner sandbox failed with exit code 1: traceback")
        self.assertEqual(loaded.planner_summaries[-1].status, "failed")
        self.assertEqual(loaded.checkpoint.sequence, 2)

    def test_plan_feedback_and_purge_use_dts_instance_operations(self) -> None:
        backend = InMemoryRuntimeStorageBackend()
        client = _FakeDtsClient()
        coordinator = DtsSwarmCoordinator(client, DurableRunOwnershipStore(backend))
        now = datetime.now(UTC)
        run = SwarmRunState(
            id="run-456",
            owner=_owner(),
            title="Awaiting plan review",
            prompt="Wait for human review",
            repository_url="https://github.com/octo/repo",
            options=_options(),
            runtime_status="Running",
            status="WaitingForPlanReview",
            phase="PlanReview",
            message="Plan ready for review.",
            awaiting_plan_review=True,
            created_at_utc=now,
            last_updated_at_utc=now,
        )
        client.states[run.id] = OrchestrationState(
            instance_id=run.id,
            name="swarm_orchestration",
            runtime_status=OrchestrationStatus.RUNNING,
            created_at=now,
            last_updated_at=now,
            serialized_input=run.model_dump_json(),
            serialized_output=None,
            serialized_custom_status=run.model_dump_json(),
            failure_details=None,
        )
        _run(DurableRunOwnershipStore(backend).add(run))

        feedback = _run(
            coordinator.submit_plan_feedback(
                run.id,
                PlanFeedbackSubmission(action="Approved"),
            )
        )
        removed = _run(coordinator.purge_run(run.id))

        self.assertIsNotNone(feedback)
        self.assertEqual(len(client.events), 1)
        self.assertEqual(client.events[0][0], planning_instance_id(_owner().user_id, run.id))
        self.assertEqual(client.events[0][1], PLAN_REVIEW_EVENT_NAME)
        self.assertEqual(client.events[0][2]["action"], "Approved")
        self.assertTrue(removed)
        self.assertEqual(client.purged, [run.id])
        self.assertIsNone(_run(backend.read_json(f"run-ownership/session-101/{run.id}.json")))

    def test_top_level_orchestrator_delegates_to_planning_then_execution_sub_orchestrations(self) -> None:
        run = SwarmRunState(
            id="run-native",
            owner=_owner(),
            title="DTS native orchestration",
            prompt="Demonstrate DTS with ACA sandboxes.",
            repository_url="https://github.com/octo/repo",
            base_branch="main",
            options=_options(),
        )
        planned_task = SwarmTaskState(
            id="task-1",
            title="Implement runtime slice",
            status="Pending",
            summary="Run the worker stage through DTS.",
            history=[SwarmTaskStatusTransition(status="Pending")],
        )
        planned = run.model_copy(
            update={
                "runtime_status": "Running",
                "status": "Running",
                "phase": "Executing",
                "plan": SwarmPlanState(
                    design_document="Use DTS-native orchestration boundaries.",
                    tasks=[planned_task],
                ),
                "tasks": [planned_task],
            }
        )
        ctx = _FakeOrchestrationContext()
        generator = swarm_orchestration(ctx, run.model_dump(mode="json"))

        first = next(generator)
        self.assertEqual(first["kind"], "sub_orchestrator")
        self.assertEqual(first["name"], PLANNING_SUB_ORCHESTRATION_NAME)
        self.assertEqual(first["instance_id"], planning_instance_id(_owner().user_id, run.id))

        second = generator.send(planned.model_dump(mode="json"))
        self.assertEqual(second["kind"], "sub_orchestrator")
        self.assertEqual(second["name"], EXECUTION_ROUND_ORCHESTRATION_NAME)

    def test_top_level_orchestrator_propagates_failed_planning_sub_orchestration(self) -> None:
        run = SwarmRunState(
            id="run-native-fail",
            owner=_owner(),
            title="DTS native orchestration failure",
            prompt="Fail the planning sub-orchestration.",
            repository_url="https://github.com/octo/repo",
            base_branch="main",
            options=_options(),
        )
        ctx = _FakeOrchestrationContext()
        generator = swarm_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        first = next(generator)
        self.assertEqual(first["kind"], "sub_orchestrator")
        self.assertEqual(first["name"], PLANNING_SUB_ORCHESTRATION_NAME)

        with self.assertRaises(TaskFailedError):
            generator.throw(_task_failed_error("Planner sandbox failed with exit code 1"))

    def test_planning_sub_orchestration_owns_plan_review_wait(self) -> None:
        run = SwarmRunState(
            id="run-plan-review",
            owner=_owner(),
            title="Await approval",
            prompt="Wait for plan approval.",
            repository_url="https://github.com/octo/repo",
            base_branch="main",
            options=SwarmOptions(
                planning=SwarmPlanningSettings(human_review_mode=HumanReviewMode.REQUIRED),
                models=_options().models,
            ),
        )
        ctx = _FakeOrchestrationContext()
        generator = planning_sub_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        execute = next(generator)
        self.assertEqual(execute["name"], RUN_PLANNER_IN_SANDBOX_ACTIVITY_NAME)
        _assert_sandbox_retry_policy(self, execute["retry_policy"])

        wait_for_review = generator.send(
            {
                "sandbox_id": "planner-1",
                "summary": "Planner created a reviewable plan.",
                "design_document": "Use DTS-native orchestration boundaries.",
                "tasks": [
                    {
                        "id": "task-1",
                        "title": "Split planner, worker, and reviewer stages",
                        "summary": "Keep the runtime DTS-owned.",
                        "dependencies": [],
                        "branch_name": None,
                        "round_number": 1,
                    }
                ],
            }
        )
        self.assertEqual(wait_for_review["kind"], "event")
        self.assertEqual(wait_for_review["name"], PLAN_REVIEW_EVENT_NAME)
        self.assertTrue(ctx.custom_statuses[-1]["awaiting_plan_review"])
        waiting = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(waiting.phase, "PlanReview")
        self.assertEqual(waiting.branch_state.current_head.reference_type, "Branch")
        self.assertEqual(waiting.branch_state.approved_head.reference_type, "Unavailable")
        self.assertIsNone(waiting.branch_state.approved_branch_name)

    def test_planning_sub_orchestration_uses_named_planner_activity_input(self) -> None:
        run = SwarmRunState(
            id="run-plan-input",
            owner=_owner(),
            title="Lean planner input",
            prompt="Keep planner activity history focused on live contract fields.",
            repository_url="https://github.com/octo/repo",
            base_branch="main",
            options=_options(),
            pending_replan_summary="Carry reviewer context into the next planning wave.",
            pending_replan_findings=["Only send fields the planner runtime still consumes."],
            plan_feedback_history=[
                PlanFeedbackSubmission(
                    action="RequestChanges",
                    comments="Tighten the planner payload contract.",
                )
            ],
        )
        ctx = _FakeOrchestrationContext()
        generator = planning_sub_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        execute = next(generator)

        self.assertEqual(execute["name"], RUN_PLANNER_IN_SANDBOX_ACTIVITY_NAME)
        self.assertEqual(set(execute["input"]), {"role", "run"})
        self.assertEqual(execute["input"]["role"], "planner")
        self.assertNotIn("sandbox", execute["input"])
        payload = SwarmRunState.model_validate(execute["input"]["run"])
        self.assertEqual(payload.id, run.id)
        self.assertEqual(payload.base_branch, "main")
        self.assertEqual(payload.plan_feedback_history[-1].comments, "Tighten the planner payload contract.")
        self.assertEqual(
            payload.pending_replan_findings,
            ["Only send fields the planner runtime still consumes."],
        )

    def test_planning_sub_orchestration_raises_after_recording_failed_status(self) -> None:
        run = SwarmRunState(
            id="run-plan-fail",
            owner=_owner(),
            title="Planner failure",
            prompt="Surface planner failures through DTS.",
            repository_url="https://github.com/octo/repo",
            base_branch="main",
            options=_options(),
        )
        ctx = _FakeOrchestrationContext()
        generator = planning_sub_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        with self.assertRaisesRegex(RuntimeError, "planner boom"):
            next(generator)
            generator.throw(RuntimeError("planner boom"))

        failed = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(failed.runtime_status, "Failed")
        self.assertEqual(failed.phase, "Failed")
        self.assertEqual(failed.failure_message, "planner boom")
        self.assertEqual(failed.planner_summaries[-1].status, "failed")

    def test_execution_round_orchestration_runs_worker_and_reviewer_before_publishing(self) -> None:
        integration_branch = "swarm/octo/run-execution-round/integration"
        task = SwarmTaskState(
            id="task-1",
            title="Implement DTS-native runtime",
            status="Pending",
            summary="Push worker execution through DTS.",
            history=[SwarmTaskStatusTransition(status="Pending")],
        )
        run = SwarmRunState(
            id="run-execution-round",
            owner=_owner(),
            title="Execution round",
            prompt="Demonstrate DTS with ACA sandboxes.",
            repository_url="https://github.com/octo/repo",
            base_branch="main",
            options=_options(),
            runtime_status="Running",
            status="Running",
            phase="Executing",
            plan=SwarmPlanState(
                design_document="Use DTS-native orchestration boundaries.",
                tasks=[task],
            ),
            tasks=[task],
            target_branch=integration_branch,
            branch_state={"branch_name": integration_branch},
        )
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        create_worker = _single_activity(worker_batch)
        self.assertEqual(create_worker["name"], RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME)

        create_worker.complete(
            {
                "sandbox_id": "worker-1",
                "summary": "Worker finished the runtime slice.",
                "details": "Updated DTS orchestration code.",
                "branch_name": "swarm/octo/run-execution-round/task-1-r1",
                "round_number": 1,
                "head_commit_sha": "1" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "validation_summary": "Ran 1 validation command(s); 1 succeeded and 0 failed.",
                "validation_results": [
                    {
                        "command": "python -m pytest tests/test_dts_runtime.py -q",
                        "exit_code": 0,
                        "status": "Succeeded",
                        "stdout": "passed",
                        "stderr": "",
                    }
                ],
            }
        )
        merge_activity = _resume_completed_task(generator, worker_batch)
        self.assertEqual(merge_activity["name"], GIT_MERGE_ACTIVITY_NAME)
        merge_activity.complete(
            {
                "sandbox_id": "merge-1",
                "target_branch": integration_branch,
                "head_commit_sha": "2" * 40,
                "parent_commit_sha": "0" * 40,
                "merged_branch_names": ["swarm/octo/run-execution-round/task-1-r1"],
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "blocked": False,
                "blocked_reason": None,
            }
        )
        reviewer_activity = _resume_completed_task(generator, merge_activity)
        self.assertEqual(reviewer_activity["name"], RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)

        reviewer_activity.complete(
            {
                "sandbox_id": "reviewer-1",
                "outcome": "Approved",
                "summary": "Reviewer approved the execution wave.",
                "details": "Ready to publish.",
                "findings": [],
                "fix_tasks": [],
                "replan_summary": None,
                "replan_findings": [],
                "target_branch": integration_branch,
                "pull_request_url": f"https://github.com/octo/repo/compare/main...{integration_branch}?expand=1",
            }
        )
        with self.assertRaises(StopIteration) as stop:
            _resume_completed_task(generator, reviewer_activity)

        completed = SwarmRunState.model_validate(stop.exception.value)
        self.assertEqual(completed.phase, "Publishing")
        self.assertEqual(completed.total_execution_rounds, 1)
        self.assertEqual(completed.reviewer_summaries[-1].summary, "Reviewer approved the execution wave.")
        self.assertEqual(completed.target_branch, integration_branch)
        self.assertEqual(completed.branch_state.current_head_checkpoint_sequence, 4)
        self.assertEqual(completed.branch_state.reviewed_checkpoint_sequence, 4)
        self.assertEqual(completed.branch_state.approved_checkpoint_sequence, 4)
        self.assertEqual(completed.branch_state.current_head_sha, "2" * 40)
        self.assertEqual(completed.branch_state.reviewed_head_sha, "2" * 40)
        self.assertEqual(completed.branch_state.approved_head_sha, "2" * 40)
        self.assertEqual(completed.branch_state.current_head.reference_type, "Commit")
        self.assertEqual(completed.branch_state.reviewed_head.reference_type, "Commit")
        self.assertEqual(completed.branch_state.approved_head.reference_type, "Commit")
        self.assertEqual(completed.branch_state.merge_state.status, "Publishing")
        self.assertFalse(completed.branch_state.merge_state.has_unreviewed_changes)
        self.assertFalse(completed.branch_state.merge_state.has_unapproved_changes)

    def test_execution_round_orchestration_schedules_worker_activity_with_retry_policy(self) -> None:
        run = _make_execution_round_run("run-worker-retry")
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_activity = _single_activity(next(generator))
        self.assertEqual(worker_activity["name"], RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME)
        _assert_sandbox_retry_policy(self, worker_activity["retry_policy"])

    def test_execution_round_orchestration_schedules_reviewer_activity_with_retry_policy(self) -> None:
        run = _make_execution_round_run("run-reviewer-retry")
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        worker_activity = _single_activity(worker_batch)
        _assert_sandbox_retry_policy(self, worker_activity["retry_policy"])
        worker_activity.complete(
            {
                "sandbox_id": "worker-reviewer-retry-1",
                "summary": "Worker finished the runtime slice.",
                "details": "Updated DTS orchestration code.",
                "branch_name": "swarm/octo/run-reviewer-retry/task-1-r1",
                "round_number": 1,
                "head_commit_sha": "1" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "validation_summary": "Ran 1 validation command(s); 1 succeeded and 0 failed.",
                "validation_results": [],
            }
        )
        merge_activity = _resume_completed_task(generator, worker_batch)
        self.assertEqual(merge_activity["name"], GIT_MERGE_ACTIVITY_NAME)
        _assert_sandbox_retry_policy(self, merge_activity["retry_policy"])
        merge_activity.complete(
            {
                "sandbox_id": "merge-reviewer-retry-1",
                "target_branch": run.target_branch,
                "head_commit_sha": "2" * 40,
                "parent_commit_sha": "0" * 40,
                "merged_branch_names": ["swarm/octo/run-reviewer-retry/task-1-r1"],
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "blocked": False,
                "blocked_reason": None,
            }
        )
        reviewer_activity = _resume_completed_task(generator, merge_activity)
        self.assertEqual(reviewer_activity["name"], RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)
        _assert_sandbox_retry_policy(self, reviewer_activity["retry_policy"])

    def test_execution_round_orchestration_fans_out_all_ready_workers_before_review(self) -> None:
        run = _make_execution_round_run(
            "run-worker-fanout",
            tasks=[
                SwarmTaskState(
                    id="task-1",
                    title="Implement scheduling",
                    status="Pending",
                    summary="Ready for worker fan-out.",
                    history=[SwarmTaskStatusTransition(status="Pending")],
                ),
                SwarmTaskState(
                    id="task-2",
                    title="Implement status updates",
                    status="Pending",
                    summary="Also ready for worker fan-out.",
                    history=[SwarmTaskStatusTransition(status="Pending")],
                ),
                SwarmTaskState(
                    id="task-3",
                    title="Wait for review",
                    status="Pending",
                    summary="Blocked on task-1 approval.",
                    dependencies=["task-1"],
                    history=[SwarmTaskStatusTransition(status="Pending")],
                ),
            ],
        )
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        scheduling = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual([task.status for task in scheduling.tasks], ["Executing", "Executing", "Pending"])

        worker_activities = _activity_group(worker_batch)
        self.assertEqual(len(worker_activities), 2)
        self.assertEqual(
            [activity["input"]["task"]["id"] for activity in worker_activities],
            ["task-1", "task-2"],
        )

        worker_activities[1].complete(
            {
                "sandbox_id": "worker-task-2",
                "summary": "Task 2 finished.",
                "details": "Second ready worker finished first.",
                "branch_name": "swarm/octo/run-worker-fanout/task-2-r1",
                "round_number": 1,
                "head_commit_sha": "2" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "validation_summary": "pytest task-2 passed",
                "validation_results": [],
            }
        )
        worker_activities[0].complete(
            {
                "sandbox_id": "worker-task-1",
                "summary": "Task 1 finished.",
                "details": "First ready worker completed after task 2.",
                "branch_name": "swarm/octo/run-worker-fanout/task-1-r1",
                "round_number": 1,
                "head_commit_sha": "1" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["tests/test_dts_runtime.py"],
                "validation_summary": "pytest task-1 passed",
                "validation_results": [],
            }
        )

        merge_activity = _resume_completed_task(generator, worker_batch)
        self.assertEqual(merge_activity["name"], GIT_MERGE_ACTIVITY_NAME)
        self.assertEqual(
            [item["task_id"] for item in merge_activity["input"]["worker_branches"]],
            ["task-1", "task-2"],
        )
        merge_activity.complete(
            {
                "sandbox_id": "merge-task-fanout",
                "target_branch": run.target_branch,
                "head_commit_sha": "3" * 40,
                "parent_commit_sha": "0" * 40,
                "merged_branch_names": [
                    "swarm/octo/run-worker-fanout/task-1-r1",
                    "swarm/octo/run-worker-fanout/task-2-r1",
                ],
                "changed_files": [
                    "src/agent_swarm_service/orchestration/dts.py",
                    "tests/test_dts_runtime.py",
                ],
                "blocked": False,
                "blocked_reason": None,
            }
        )
        reviewer_activity = _resume_completed_task(generator, merge_activity)
        self.assertEqual(reviewer_activity["name"], RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)

        reviewing = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual([task.status for task in reviewing.tasks], ["InReview", "InReview", "Pending"])
        self.assertEqual([summary.status for summary in reviewing.worker_summaries], ["completed", "completed"])
        self.assertEqual(
            [summary.title for summary in reviewing.worker_summaries],
            ["Implement scheduling", "Implement status updates"],
        )
        self.assertEqual(reviewing.branch_state.current_head_sha, "3" * 40)

    def test_execution_round_keeps_run_head_on_integration_branch_for_parallel_worker_results(self) -> None:
        integration_branch = "swarm/octo/run-worker-fanout-head/integration"
        base_run = _make_execution_round_run(
            "run-worker-fanout-head",
            tasks=[
                SwarmTaskState(
                    id="task-1",
                    title="Implement scheduling",
                    status="Pending",
                    summary="Ready for worker fan-out.",
                    history=[SwarmTaskStatusTransition(status="Pending")],
                ),
                SwarmTaskState(
                    id="task-2",
                    title="Implement status updates",
                    status="Pending",
                    summary="Also ready for worker fan-out.",
                    history=[SwarmTaskStatusTransition(status="Pending")],
                ),
            ],
        )
        run = base_run.model_copy(
            update={
                "target_branch": integration_branch,
                "branch_state": base_run.branch_state.model_copy(
                    update={
                        "branch_name": integration_branch,
                        "current_head_sha": "f" * 40,
                        "current_head_checkpoint_sequence": 3,
                        "reviewed_head_sha": "e" * 40,
                        "reviewed_checkpoint_sequence": 2,
                        "approved_head_sha": "d" * 40,
                        "approved_checkpoint_sequence": 1,
                    }
                ),
            }
        )
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        worker_activities = _activity_group(worker_batch)
        self.assertEqual(len(worker_activities), 2)

        worker_activities[0].complete(
            {
                "sandbox_id": "worker-task-1",
                "summary": "Task 1 finished.",
                "details": "First worker completed on its task branch.",
                "branch_name": "swarm/octo/run-worker-fanout-head/task-1-r1",
                "round_number": 1,
                "head_commit_sha": "1" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "validation_summary": "pytest task-1 passed",
                "validation_results": [],
            }
        )
        worker_activities[1].complete(
            {
                "sandbox_id": "worker-task-2",
                "summary": "Task 2 finished.",
                "details": "Second worker completed on its task branch.",
                "branch_name": "swarm/octo/run-worker-fanout-head/task-2-r1",
                "round_number": 1,
                "head_commit_sha": "2" * 40,
                "parent_commit_sha": "f" * 40,
                "changed_files": ["tests/test_dts_runtime.py"],
                "validation_summary": "pytest task-2 passed",
                "validation_results": [],
            }
        )

        merge_activity = _resume_completed_task(generator, worker_batch)
        self.assertEqual(merge_activity["name"], GIT_MERGE_ACTIVITY_NAME)

        review_ready = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(review_ready.target_branch, integration_branch)
        self.assertEqual(review_ready.branch_state.branch_name, integration_branch)
        self.assertEqual(review_ready.branch_state.current_head_sha, "f" * 40)
        self.assertEqual(review_ready.branch_state.current_head_checkpoint_sequence, 3)
        self.assertEqual([task.status for task in review_ready.tasks], ["PendingReview", "PendingReview"])
        self.assertEqual(
            [task.branch_name for task in review_ready.tasks],
            [
                "swarm/octo/run-worker-fanout-head/task-1-r1",
                "swarm/octo/run-worker-fanout-head/task-2-r1",
            ],
        )
        self.assertEqual(
            [task.head_commit_sha for task in review_ready.tasks],
            ["1" * 40, "2" * 40],
        )
        self.assertEqual(
            [item["task_id"] for item in merge_activity["input"]["worker_branches"]],
            ["task-1", "task-2"],
        )
        merge_activity.complete(
            {
                "sandbox_id": "merge-fanout-head",
                "target_branch": integration_branch,
                "head_commit_sha": "3" * 40,
                "parent_commit_sha": "f" * 40,
                "merged_branch_names": [
                    "swarm/octo/run-worker-fanout-head/task-1-r1",
                    "swarm/octo/run-worker-fanout-head/task-2-r1",
                ],
                "changed_files": [
                    "src/agent_swarm_service/orchestration/dts.py",
                    "tests/test_dts_runtime.py",
                ],
                "blocked": False,
                "blocked_reason": None,
            }
        )
        reviewer_activity = _resume_completed_task(generator, merge_activity)
        self.assertEqual(reviewer_activity["name"], RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)
        self.assertEqual(reviewer_activity["input"]["run"]["target_branch"], integration_branch)
        self.assertEqual(reviewer_activity["input"]["run"]["branch_state"]["branch_name"], integration_branch)
        self.assertEqual(reviewer_activity["input"]["run"]["branch_state"]["current_head_sha"], "3" * 40)
        self.assertEqual(
            [task["status"] for task in reviewer_activity["input"]["run"]["tasks"]],
            ["InReview", "InReview"],
        )
        self.assertEqual(
            [task["branch_name"] for task in reviewer_activity["input"]["run"]["tasks"]],
            [
                "swarm/octo/run-worker-fanout-head/task-1-r1",
                "swarm/octo/run-worker-fanout-head/task-2-r1",
            ],
        )

        reviewing = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(reviewing.branch_state.current_head_sha, "3" * 40)

    def test_execution_round_orchestration_raises_after_worker_failure(self) -> None:
        run = _make_execution_round_run("run-worker-fail")
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        worker_activity = _single_activity(worker_batch)
        self.assertIsInstance(worker_activity["retry_policy"], RetryPolicy)
        worker_activity.fail_with("worker boom")

        with self.assertRaisesRegex(TaskFailedError, "worker boom"):
            _resume_completed_task(generator, worker_batch)

        failed = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(failed.runtime_status, "Failed")
        self.assertEqual(failed.failure_message, "worker boom")
        self.assertEqual(failed.tasks[0].status, "Failed")
        self.assertEqual(failed.tasks[0].failure_details, "worker boom")
        self.assertEqual(failed.worker_summaries[-1].status, "failed")

    def test_execution_round_orchestration_fails_batched_worker_wave_without_partial_apply(self) -> None:
        run = _make_execution_round_run(
            "run-worker-wave-fail",
            tasks=[
                SwarmTaskState(
                    id="task-1",
                    title="Finish worker slice",
                    status="Pending",
                    summary="Ready for execution.",
                    history=[SwarmTaskStatusTransition(status="Pending")],
                ),
                SwarmTaskState(
                    id="task-2",
                    title="Fail after retries",
                    status="Pending",
                    summary="Also ready for execution.",
                    history=[SwarmTaskStatusTransition(status="Pending")],
                ),
            ],
        )
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        worker_activities = _activity_group(worker_batch)
        self.assertEqual(len(worker_activities), 2)

        worker_activities[0].complete(
            {
                "sandbox_id": "worker-task-1",
                "summary": "Task 1 finished.",
                "details": "Completed before the other worker exhausted retries.",
                "branch_name": "swarm/octo/run-worker-wave-fail/task-1-r1",
                "round_number": 1,
                "head_commit_sha": "1" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "validation_summary": "pytest task-1 passed",
                "validation_results": [],
            }
        )
        worker_activities[1].fail_with("worker boom after retries")

        with self.assertRaisesRegex(TaskFailedError, "worker boom after retries"):
            _resume_completed_task(generator, worker_batch)

        failed = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(failed.runtime_status, "Failed")
        self.assertEqual(failed.failure_message, "worker boom after retries")
        self.assertEqual([task.status for task in failed.tasks], ["Failed", "Failed"])
        self.assertEqual(
            failed.tasks[0].summary,
            "Worker wave failed before this result could be applied.",
        )
        self.assertEqual(
            failed.tasks[0].failure_details,
            "Worker wave failed after another worker exhausted retries: worker boom after retries",
        )
        self.assertEqual(failed.tasks[1].failure_details, "worker boom after retries")
        self.assertEqual(
            [transition.status for transition in failed.tasks[0].history],
            ["Pending", "Executing", "Failed"],
        )
        self.assertEqual(
            [transition.status for transition in failed.tasks[1].history],
            ["Pending", "Executing", "Failed"],
        )
        self.assertIsNone(failed.tasks[0].branch_name)
        self.assertIsNone(failed.tasks[0].head_commit_sha)
        self.assertEqual(failed.tasks[0].changed_files, [])
        self.assertIsNone(failed.tasks[0].validation_summary)
        self.assertEqual([summary.status for summary in failed.worker_summaries], ["failed", "failed"])
        self.assertEqual(
            failed.worker_summaries[0].summary,
            "Worker wave failed before this result could be applied.",
        )
        self.assertEqual(
            failed.worker_summaries[0].details,
            "Worker wave failed after another worker exhausted retries: worker boom after retries",
        )
        self.assertIsNone(failed.worker_summaries[0].branch_name)
        self.assertIsNone(failed.worker_summaries[0].head_commit_sha)
        self.assertEqual(failed.worker_summaries[0].changed_files, [])
        self.assertIsNone(failed.branch_state.current_head_sha)

    def test_execution_round_orchestration_raises_after_reviewer_failure(self) -> None:
        run = _make_execution_round_run("run-reviewer-fail")
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        worker_activity = _single_activity(worker_batch)
        worker_activity.complete(
            {
                "sandbox_id": "worker-reviewer-fail-1",
                "summary": "Worker finished the runtime slice.",
                "details": "Updated DTS orchestration code.",
                "branch_name": "swarm/octo/run-reviewer-fail/task-1-r1",
                "round_number": 1,
                "head_commit_sha": "1" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "validation_summary": "Ran 1 validation command(s); 1 succeeded and 0 failed.",
                "validation_results": [],
            }
        )
        merge_activity = _resume_completed_task(generator, worker_batch)
        self.assertEqual(merge_activity["name"], GIT_MERGE_ACTIVITY_NAME)
        merge_activity.complete(
            {
                "sandbox_id": "merge-reviewer-fail-1",
                "target_branch": run.target_branch,
                "head_commit_sha": "2" * 40,
                "parent_commit_sha": "0" * 40,
                "merged_branch_names": ["swarm/octo/run-reviewer-fail/task-1-r1"],
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "blocked": False,
                "blocked_reason": None,
            }
        )
        reviewer_activity = _resume_completed_task(generator, merge_activity)
        self.assertEqual(reviewer_activity["name"], RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)
        reviewer_activity.fail_with("reviewer boom")

        with self.assertRaisesRegex(TaskFailedError, "reviewer boom"):
            _resume_completed_task(generator, reviewer_activity)

        failed = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(failed.runtime_status, "Failed")
        self.assertEqual(failed.failure_message, "reviewer boom")
        self.assertEqual(failed.reviewer_summaries[-1].status, "failed")

    def test_execution_round_fails_before_reviewer_when_git_merge_activity_fails(self) -> None:
        run = _make_execution_round_run("run-merge-fail")
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        worker_activity = _single_activity(worker_batch)
        worker_activity.complete(
            {
                "sandbox_id": "worker-merge-fail-1",
                "summary": "Worker finished the runtime slice.",
                "details": "Updated DTS orchestration code.",
                "branch_name": "swarm/octo/run-merge-fail/task-1-r1",
                "round_number": 1,
                "head_commit_sha": "1" * 40,
                "parent_commit_sha": "0" * 40,
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "validation_summary": "Ran 1 validation command(s); 1 succeeded and 0 failed.",
                "validation_results": [],
            }
        )
        merge_activity = _resume_completed_task(generator, worker_batch)
        self.assertEqual(merge_activity["name"], GIT_MERGE_ACTIVITY_NAME)
        merge_activity.fail_with("merge boom")

        with self.assertRaisesRegex(TaskFailedError, "merge boom"):
            _resume_completed_task(generator, merge_activity)

        failed = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(failed.runtime_status, "Failed")
        self.assertEqual(failed.phase, "Failed")
        self.assertEqual(failed.failure_message, "merge boom")
        self.assertEqual(failed.tasks[0].status, "Failed")
        self.assertEqual(
            failed.tasks[0].summary,
            "Git merge activity failed before reviewer validation.",
        )
        self.assertEqual(failed.tasks[0].failure_details, "merge boom")
        self.assertEqual(failed.branch_state.merge_state.status, "Failed")
        self.assertEqual(failed.reviewer_summaries, [])

    def test_execution_round_fix_tasks_stays_in_same_wave_on_run_head_branch(self) -> None:
        task = SwarmTaskState(
            id="task-1",
            title="Harden DTS runtime",
            status="PendingReview",
            summary="Ready for reviewer feedback.",
            branch_name="swarm/octo/run-fix-round/integration",
            round_number=1,
            history=[
                SwarmTaskStatusTransition(status="Pending"),
                SwarmTaskStatusTransition(status="Executing"),
                SwarmTaskStatusTransition(status="PendingReview"),
            ],
        )
        run = SwarmRunState(
            id="run-fix-round",
            owner=_owner(),
            title="Fix round",
            prompt="Need hardening review gap fix-chain work.",
            repository_url="https://github.com/octo/repo",
            base_branch="main",
            target_branch="swarm/octo/run-fix-round/integration",
            options=_options(),
            runtime_status="Running",
            status="Running",
            phase="Reviewing",
            plan=SwarmPlanState(design_document="Review the current run head.", tasks=[task]),
            tasks=[task],
            branch_state={
                "branch_name": "swarm/octo/run-fix-round/integration",
                "current_head_sha": "4" * 40,
                "current_head_checkpoint_sequence": 4,
                "active_wave": 2,
                "current_wave_round": 0,
            },
        )
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        reviewer_activity = next(generator)
        self.assertEqual(reviewer_activity["name"], RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)

        with self.assertRaises(StopIteration) as stop:
            generator.send(
            {
                "sandbox_id": "reviewer-fix",
                "outcome": "FixTasks",
                "summary": "Reviewer queued fix tasks on the run head branch.",
                "details": "Keep iterating in the same wave.",
                "findings": [],
                "fix_tasks": [
                    {
                        "id": "fix-task-1-1",
                        "title": "Address reviewer gap",
                        "description": "Tighten the runtime slice.",
                        "dependencies": ["task-1"],
                        "round_number": 2,
                        "branch_name": "swarm/octo/run-fix-round/integration",
                    }
                ],
                "replan_summary": None,
                "replan_findings": [],
                "target_branch": "swarm/octo/run-fix-round/integration",
                "pull_request_url": None,
            }
        )

        queued = SwarmRunState.model_validate(stop.exception.value)
        self.assertEqual(queued.phase, "Executing")
        self.assertEqual(queued.branch_state.active_wave, 2)
        self.assertEqual(queued.branch_state.current_wave_round, 1)
        self.assertEqual(queued.branch_state.current_head_checkpoint_sequence, 4)
        self.assertEqual(queued.branch_state.reviewed_checkpoint_sequence, 4)
        self.assertIsNone(queued.branch_state.approved_checkpoint_sequence)
        self.assertEqual(queued.branch_state.current_head_sha, "4" * 40)
        self.assertEqual(queued.branch_state.reviewed_head_sha, "4" * 40)
        self.assertIsNone(queued.branch_state.approved_head_sha)
        self.assertEqual(queued.tasks[-1].branch_name, "swarm/octo/run-fix-round/integration")
        self.assertEqual(queued.branch_state.merge_state.status, "Executing")
        self.assertFalse(queued.branch_state.merge_state.has_unreviewed_changes)
        self.assertTrue(queued.branch_state.merge_state.has_unapproved_changes)

    def test_execution_round_replan_starts_new_wave_from_reviewed_head(self) -> None:
        task = SwarmTaskState(
            id="task-1",
            title="Re-sequence runtime work",
            status="PendingReview",
            summary="Ready for reviewer feedback.",
            branch_name="swarm/octo/run-replan/integration",
            round_number=1,
            history=[
                SwarmTaskStatusTransition(status="Pending"),
                SwarmTaskStatusTransition(status="Executing"),
                SwarmTaskStatusTransition(status="PendingReview"),
            ],
        )
        run = SwarmRunState(
            id="run-replan",
            owner=_owner(),
            title="Replan",
            prompt="Please replan this backend slice.",
            repository_url="https://github.com/octo/repo",
            base_branch="main",
            target_branch="swarm/octo/run-replan/integration",
            options=_options(),
            runtime_status="Running",
            status="Running",
            phase="Reviewing",
            plan=SwarmPlanState(design_document="Initial wave.", tasks=[task]),
            tasks=[task],
            branch_state={
                "branch_name": "swarm/octo/run-replan/integration",
                "current_head_sha": "6" * 40,
                "current_head_checkpoint_sequence": 6,
                "active_wave": 1,
                "current_wave_round": 0,
            },
        )
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        reviewer_activity = next(generator)
        self.assertEqual(reviewer_activity["name"], RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)

        with self.assertRaises(StopIteration) as stop:
            generator.send(
            {
                "sandbox_id": "reviewer-replan",
                "outcome": "Replan",
                "summary": "Reviewer requested a new planning epoch.",
                "details": "Start a new wave from the current reviewed head.",
                "findings": [],
                "fix_tasks": [],
                "replan_summary": "Re-think the runtime sequencing.",
                "replan_findings": ["Use the reviewed head as the new planning base."],
                "target_branch": "swarm/octo/run-replan/integration",
                "pull_request_url": None,
            }
        )

        replanned = SwarmRunState.model_validate(stop.exception.value)
        self.assertEqual(replanned.phase, "Planning")
        self.assertEqual(replanned.branch_state.active_wave, 2)
        self.assertEqual(replanned.branch_state.current_wave_round, 0)
        self.assertEqual(replanned.branch_state.current_head_checkpoint_sequence, 6)
        self.assertEqual(replanned.branch_state.reviewed_checkpoint_sequence, 6)
        self.assertIsNone(replanned.branch_state.approved_checkpoint_sequence)
        self.assertEqual(replanned.branch_state.current_head_sha, "6" * 40)
        self.assertEqual(replanned.branch_state.reviewed_head_sha, "6" * 40)
        self.assertEqual(replanned.branch_state.merge_state.status, "ReplanRequested")
        self.assertFalse(replanned.branch_state.merge_state.has_unreviewed_changes)
        self.assertTrue(replanned.branch_state.merge_state.has_unapproved_changes)
        self.assertIsNone(replanned.branch_state.approved_head_sha)
        self.assertEqual(replanned.target_branch, "swarm/octo/run-replan/integration")

    def test_fix_tasks_queue_same_wave_follow_up_round_without_replanning(self) -> None:
        queued = _complete_execution_round(
            _make_execution_round_run("run-fix-wave"),
            review_overrides={
                "outcome": "FixTasks",
                "summary": "Reviewer requested same-wave fixes.",
                "details": "Complete the follow-up task and rerun review before publish.",
                "fix_tasks": [
                    {
                        "id": "task-1-fix",
                        "title": "Close reviewer findings",
                        "description": "Address the review comments on the same integration branch.",
                        "dependencies": ["task-1"],
                        "round_number": 2,
                        "branch_name": "swarm/octo/run-fix-wave/integration",
                        "target_files": ["README.md"],
                        "acceptance_criteria": ["README.md explains the updated reviewer guidance."],
                    }
                ],
            },
        )

        self.assertEqual(queued.phase, "Executing")
        self.assertEqual(queued.total_execution_rounds, 1)
        self.assertEqual(queued.consecutive_fix_rounds, 1)
        self.assertEqual(queued.replan_count, 0)
        self.assertEqual(queued.plan.design_document, "Use DTS-native orchestration boundaries.")
        self.assertEqual([task.id for task in queued.tasks], ["task-1", "task-1-fix"])
        self.assertEqual(queued.tasks[0].status, "Completed")
        self.assertEqual(queued.tasks[1].status, "Pending")
        self.assertEqual(queued.tasks[1].round_number, 2)
        self.assertEqual(queued.branch_state.active_wave, 1)
        self.assertEqual(queued.branch_state.current_wave_round, 1)
        self.assertEqual(queued.branch_state.current_head_checkpoint_sequence, 4)
        self.assertEqual(queued.branch_state.reviewed_checkpoint_sequence, 4)
        self.assertIsNone(queued.branch_state.approved_checkpoint_sequence)
        self.assertEqual(queued.branch_state.current_head_sha, "1" * 40)
        self.assertEqual(queued.branch_state.reviewed_head_sha, "1" * 40)
        self.assertIsNone(queued.branch_state.approved_head_sha)
        self.assertEqual(queued.reviewer_summaries[-1].fix_tasks[0].branch_name, "swarm/octo/run-fix-wave/integration")
        self.assertEqual(queued.tasks[1].target_files, ["README.md"])
        self.assertEqual(queued.tasks[1].acceptance_criteria, ["README.md explains the updated reviewer guidance."])

        ctx = _FakeOrchestrationContext()
        generator = swarm_orchestration(ctx, {"run": queued.model_dump(mode="json"), "history": {}})

        first = next(generator)

        self.assertEqual(first["kind"], "sub_orchestrator")
        self.assertEqual(first["name"], EXECUTION_ROUND_ORCHESTRATION_NAME)
        self.assertEqual(first["instance_id"], execution_round_instance_id(_owner().user_id, queued.id, 2))

    def test_follow_up_fix_round_advances_current_head_before_reviewed_head_catches_up(self) -> None:
        queued = _complete_execution_round(
            _make_execution_round_run("run-fix-head-gap"),
            review_overrides={
                "outcome": "FixTasks",
                "summary": "Reviewer requested same-wave fixes.",
                "details": "Keep iterating on the integration branch before publish.",
                "fix_tasks": [
                    {
                        "id": "task-1-fix",
                        "title": "Close reviewer findings",
                        "description": "Address the review comments on the same integration branch.",
                        "dependencies": ["task-1"],
                        "round_number": 2,
                        "branch_name": "swarm/octo/run-fix-head-gap/integration",
                    }
                ],
            },
        )

        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": queued.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        worker_activity = _single_activity(worker_batch)
        self.assertEqual(worker_activity["name"], RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME)

        worker_activity.complete(
            {
                "sandbox_id": "worker-2",
                "summary": "Worker finished the same-wave fix round.",
                "details": "Updated the long-lived integration branch with the follow-up change.",
                "branch_name": "swarm/octo/run-fix-head-gap/integration",
                "round_number": 2,
                "head_commit_sha": "2" * 40,
                "parent_commit_sha": "1" * 40,
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "validation_summary": "Ran 1 validation command(s); 1 succeeded and 0 failed.",
                "validation_results": [
                    {
                        "command": "python -m pytest tests/test_dts_runtime.py -q",
                        "exit_code": 0,
                        "status": "Succeeded",
                        "stdout": "passed",
                        "stderr": "",
                    }
                ],
            }
        )
        merge_activity = _resume_completed_task(generator, worker_batch)
        self.assertEqual(merge_activity["name"], GIT_MERGE_ACTIVITY_NAME)
        merge_activity.complete(
            {
                "sandbox_id": "merge-2",
                "target_branch": "swarm/octo/run-fix-head-gap/integration",
                "head_commit_sha": "2" * 40,
                "parent_commit_sha": "1" * 40,
                "merged_branch_names": ["swarm/octo/run-fix-head-gap/integration"],
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "blocked": False,
                "blocked_reason": None,
            }
        )
        reviewer_activity = _resume_completed_task(generator, merge_activity)
        self.assertEqual(reviewer_activity["name"], RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)

        reviewing = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(reviewing.phase, "Reviewing")
        self.assertEqual(reviewing.branch_state.active_wave, 1)
        self.assertEqual(reviewing.branch_state.current_wave_round, 1)
        self.assertGreater(
            reviewing.branch_state.current_head_checkpoint_sequence,
            reviewing.branch_state.reviewed_checkpoint_sequence,
        )
        self.assertEqual(reviewing.branch_state.reviewed_checkpoint_sequence, 4)
        self.assertIsNone(reviewing.branch_state.approved_checkpoint_sequence)
        self.assertEqual(reviewing.branch_state.current_head_sha, "2" * 40)
        self.assertEqual(reviewing.branch_state.reviewed_head_sha, "1" * 40)
        self.assertIsNone(reviewing.branch_state.approved_head_sha)
        self.assertEqual(reviewing.tasks[-1].status, "InReview")
        self.assertEqual(reviewing.tasks[-1].branch_name, "swarm/octo/run-fix-head-gap/integration")

    def test_replan_queues_new_planning_epoch_with_reviewer_guidance(self) -> None:
        replanned = _complete_execution_round(
            _make_execution_round_run("run-replan-wave"),
            review_overrides={
                "outcome": "Replan",
                "summary": "Reviewer requested a new execution wave.",
                "details": "Return to planning with the current reviewer guidance.",
                "replan_summary": "Create a new wave from the current execution head.",
                "replan_findings": ["Preserve the completed implementation context in the next plan."],
            },
        )

        self.assertEqual(replanned.phase, "Planning")
        self.assertEqual(replanned.total_execution_rounds, 1)
        self.assertEqual(replanned.replan_count, 1)
        self.assertEqual(replanned.consecutive_fix_rounds, 0)
        self.assertIsNone(replanned.plan.design_document)
        self.assertEqual(replanned.tasks, [])
        self.assertEqual(replanned.pending_replan_summary, "Create a new wave from the current execution head.")
        self.assertEqual(
            replanned.pending_replan_findings,
            ["Preserve the completed implementation context in the next plan."],
        )
        self.assertEqual(
            replanned.reviewer_summaries[-1].replan_summary,
            "Create a new wave from the current execution head.",
        )

        ctx = _FakeOrchestrationContext()
        generator = swarm_orchestration(ctx, {"run": replanned.model_dump(mode="json"), "history": {}})

        first = next(generator)

        self.assertEqual(first["kind"], "sub_orchestrator")
        self.assertEqual(first["name"], PLANNING_SUB_ORCHESTRATION_NAME)
        self.assertEqual(first["instance_id"], replan_instance_id(_owner().user_id, replanned.id, 1))

    def test_publish_projection_keeps_approval_and_publish_details_coherent(self) -> None:
        approved = _complete_execution_round(_make_execution_round_run("run-publish-model"), review_overrides={})
        self.assertEqual(approved.branch_state.current_head_checkpoint_sequence, 4)
        self.assertEqual(approved.branch_state.reviewed_checkpoint_sequence, 4)
        self.assertEqual(approved.branch_state.approved_checkpoint_sequence, 4)
        self.assertEqual(approved.branch_state.current_head_sha, "1" * 40)
        self.assertEqual(approved.branch_state.reviewed_head_sha, "1" * 40)
        self.assertEqual(approved.branch_state.approved_head_sha, "1" * 40)
        ctx = _FakeOrchestrationContext()
        generator = swarm_orchestration(ctx, approved.model_dump(mode="json"))

        publish = next(generator)
        self.assertEqual(publish["name"], PUBLISH_TO_GITHUB_ACTIVITY_NAME)

        with self.assertRaises(StopIteration) as stop:
            generator.send(
                {
                    "status": "Published",
                    "target_branch": approved.target_branch,
                    "pull_request_url": "https://github.com/octo/repo/pull/1234",
                    "pull_request_number": 1234,
                    "commit_sha": approved.branch_state.current_head_sha,
                }
            )

        completed = SwarmRunState.model_validate(stop.exception.value)
        snapshot = build_projection_snapshot(completed)

        self.assertEqual(completed.phase, "Completed")
        self.assertEqual(completed.publish_status, "Published")
        self.assertEqual(completed.target_branch, approved.target_branch)
        self.assertEqual(completed.pull_request_number, 1234)
        self.assertEqual(completed.branch_state.current_head_checkpoint_sequence, 4)
        self.assertEqual(completed.branch_state.reviewed_checkpoint_sequence, 4)
        self.assertEqual(completed.branch_state.approved_checkpoint_sequence, 4)
        self.assertEqual(completed.branch_state.current_head_sha, "1" * 40)
        self.assertEqual(completed.branch_state.reviewed_head_sha, "1" * 40)
        self.assertEqual(completed.branch_state.approved_head_sha, "1" * 40)
        self.assertEqual(
            completed.reviewer_summaries[-1].summary,
            "Reviewer approved and published the execution wave for https://github.com/octo/repo.",
        )
        self.assertEqual(completed.reviewer_summaries[-1].publish_status, "Published")
        self.assertEqual(snapshot.details["publishStatus"], "Published")
        self.assertEqual(snapshot.details["targetBranch"], approved.target_branch)
        self.assertEqual(snapshot.details["pullRequestNumber"], 1234)
        self.assertEqual(snapshot.details["branchState"]["currentHeadCheckpointSequence"], 4)
        self.assertEqual(snapshot.details["branchState"]["reviewedCheckpointSequence"], 4)
        self.assertEqual(snapshot.details["branchState"]["approvedCheckpointSequence"], 4)
        self.assertEqual(snapshot.details["branchState"]["currentHeadSha"], "1" * 40)
        self.assertEqual(snapshot.details["branchState"]["reviewedHeadSha"], "1" * 40)
        self.assertEqual(snapshot.details["branchState"]["approvedHeadSha"], "1" * 40)
        self.assertEqual(snapshot.details["branchState"]["currentHead"]["referenceType"], "Commit")
        self.assertEqual(snapshot.details["branchState"]["reviewedHead"]["referenceType"], "Commit")
        self.assertEqual(snapshot.details["branchState"]["approvedHead"]["referenceType"], "Commit")
        self.assertEqual(snapshot.details["branchState"]["mergeState"]["status"], "Published")
        self.assertFalse(snapshot.details["branchState"]["mergeState"]["hasUnreviewedChanges"])
        self.assertFalse(snapshot.details["branchState"]["mergeState"]["hasUnapprovedChanges"])
        self.assertEqual(snapshot.details["reviewerSummaries"][-1]["publishStatus"], "Published")
        self.assertEqual(snapshot.details["reviewerSummaries"][-1]["pullRequestNumber"], 1234)

    def test_publish_result_does_not_overwrite_integration_branch_with_worker_branch_metadata(self) -> None:
        approved = _complete_execution_round(_make_execution_round_run("run-publish-worker-branch"), review_overrides={})
        self.assertEqual(approved.tasks[0].branch_name, "swarm/octo/run-publish-worker-branch/task-1-r1")
        self.assertEqual(approved.tasks[0].head_commit_sha, "1" * 40)
        self.assertEqual(approved.target_branch, "swarm/octo/run-publish-worker-branch/integration")
        self.assertEqual(approved.branch_state.current_head_sha, "1" * 40)
        ctx = _FakeOrchestrationContext()
        generator = swarm_orchestration(ctx, approved.model_dump(mode="json"))

        publish = next(generator)
        self.assertEqual(publish["name"], PUBLISH_TO_GITHUB_ACTIVITY_NAME)

        with self.assertRaises(StopIteration) as stop:
            generator.send(
                {
                    "status": "Published",
                    "target_branch": approved.tasks[0].branch_name,
                    "pull_request_url": "https://github.com/octo/repo/pull/5678",
                    "pull_request_number": 5678,
                    "commit_sha": "f" * 40,
                }
            )

        completed = SwarmRunState.model_validate(stop.exception.value)

        self.assertEqual(completed.target_branch, approved.target_branch)
        self.assertEqual(completed.branch_state.branch_name, approved.target_branch)
        self.assertEqual(completed.branch_state.current_head_sha, approved.branch_state.current_head_sha)
        self.assertEqual(completed.branch_state.reviewed_head_sha, approved.branch_state.reviewed_head_sha)
        self.assertEqual(completed.branch_state.approved_branch_name, approved.target_branch)
        self.assertEqual(completed.branch_state.approved_head_sha, approved.branch_state.approved_head_sha)

    def test_branch_state_discards_preseeded_approved_branch_without_approval_artifact(self) -> None:
        branch_state = SwarmRunState(
            id="run-approved-head-sanitized",
            owner=_owner(),
            title="Approved head sanitization",
            prompt="Do not expose approved head before approval exists.",
            repository_url="https://github.com/octo/repo",
            target_branch="swarm/octo/run-approved-head-sanitized/integration",
            options=_options(),
            branch_state={
                "branch_name": "swarm/octo/run-approved-head-sanitized/integration",
                "approved_branch_name": "swarm/octo/run-approved-head-sanitized/integration",
            },
        ).branch_state

        self.assertIsNone(branch_state.approved_branch_name)
        self.assertEqual(branch_state.approved_head.reference_type, "Unavailable")
        self.assertIsNone(branch_state.approved_head.branch_name)

    def test_sandbox_executor_retries_for_fresh_run_secret_visibility_race(self) -> None:
        now = datetime.now(UTC)
        run = SwarmRunState(
            id="run-secret-race",
            owner=_owner(),
            title="Secret race",
            prompt="Reproduce the run-secret visibility race.",
            repository_url="https://github.com/octo/repo",
            options=_options(),
            created_at_utc=now,
            last_updated_at_utc=now,
        )
        store = _DelayedRunSecretStore(run.id, missing_attempts=2)
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=object(),
            run_secret_store=store,
            run_secret_retry_attempts=4,
            run_secret_retry_delay_seconds=0.0,
            run_secret_retry_window_seconds=60.0,
        )

        environment = _run(executor.build_execution_environment(run))

        self.assertEqual(environment["GH_TOKEN"], "ghp_race_token")
        self.assertEqual(environment["GITHUB_TOKEN"], "ghp_race_token")
        self.assertNotIn("COPILOT_GITHUB_TOKEN", environment)
        self.assertEqual(environment["GIT_TERMINAL_PROMPT"], "0")
        self.assertEqual(environment["GCM_INTERACTIVE"], "Never")
        self.assertEqual(environment["SWARM_COPILOT_RUNTIME"], "github-copilot-sdk")
        self.assertEqual(environment["SWARM_COPILOT_AUTH_MODE"], "run-scoped-pat")
        self.assertEqual(environment["SWARM_COPILOT_TOKEN_ENV_VAR"], "GH_TOKEN")
        self.assertEqual(environment["SWARM_COPILOT_USE_LOGGED_IN_USER"], "false")
        self.assertEqual(store.get_calls, 3)

    def test_build_execution_environment_respects_explicit_copilot_token_env_override(self) -> None:
        settings = ServiceSettings.for_local_development()
        runtime_defaults = settings.runtime.model_copy(
            update={"copilot_token_environment_variable": "COPILOT_GITHUB_TOKEN"}
        )
        run = SwarmRunState(
            id="swarm-env-override",
            owner=_owner(),
            title="Repro the env contract",
            prompt="Use the Copilot token env requested by the runtime.",
            repository_url="https://github.com/octo/repo",
            options=runtime_defaults.to_swarm_options(),
        )
        store = InMemoryRunSecretStore()
        _run(store.store(build_run_secret(run.id, "ghp_override_token", lifetime=timedelta(hours=1))))
        executor = AcaSandboxLifecycleExecutor(
            settings,
            sandbox_client=object(),
            run_secret_store=store,
        )

        environment = _run(executor.build_execution_environment(run))

        self.assertEqual(environment["GH_TOKEN"], "ghp_override_token")
        self.assertEqual(environment["GITHUB_TOKEN"], "ghp_override_token")
        self.assertEqual(environment["COPILOT_GITHUB_TOKEN"], "ghp_override_token")
        self.assertEqual(environment["SWARM_COPILOT_TOKEN_ENV_VAR"], "COPILOT_GITHUB_TOKEN")

    def test_sandbox_request_matches_direct_sdk_create_signature(self) -> None:
        disk_id = "/subscriptions/000/resourceGroups/rg/providers/Microsoft.App/diskImages/private-image"
        options = _options()
        run = SwarmRunState(
            id="run-create-sandbox",
            owner=_owner(),
            title="Create sandbox",
            prompt="Use the direct ACA SDK create_sandbox signature.",
            repository_url="https://github.com/octo/repo",
            options=options.model_copy(
                update={
                    "sandbox": options.sandbox.model_copy(update={"sandbox_disk_id": disk_id}),
                }
            ),
        )
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=object(),
            run_secret_store=_DelayedRunSecretStore(run.id, missing_attempts=0),
        )

        request = executor.build_sandbox_request("worker", run)
        supported_parameters = set(inspect.signature(SandboxClient.create_sandbox).parameters) - {"self"}

        self.assertNotIn("working_directory", request)
        self.assertNotIn("preset", request)
        self.assertNotIn("snapshot_id", request)
        self.assertNotIn("disk", request)
        self.assertEqual(request["disk_id"], disk_id)
        self.assertLessEqual(set(request), supported_parameters)

    def test_sandbox_request_forwards_supported_selector_kwargs_only(self) -> None:
        supported_parameters = set(inspect.signature(SandboxClient.create_sandbox).parameters) - {"self"}
        disk_id = "/subscriptions/000/resourceGroups/rg/providers/Microsoft.App/diskImages/private-image"
        options = _options()
        sandbox = options.sandbox.__class__.model_validate(
            {
                **options.sandbox.model_dump(mode="python"),
                "sandbox_disk_id": disk_id,
            }
        )
        run = SwarmRunState(
            id="run-sandbox-disk-id",
            owner=_owner(),
            title="Create sandbox",
            prompt="Use the direct ACA SDK create_sandbox signature.",
            repository_url="https://github.com/octo/repo",
            options=options.model_copy(update={"sandbox": sandbox}),
        )
        executor = AcaSandboxLifecycleExecutor(
            ServiceSettings.for_local_development(),
            sandbox_client=object(),
            run_secret_store=_DelayedRunSecretStore(run.id, missing_attempts=0),
        )

        request = executor.build_sandbox_request("worker", run)

        self.assertDictEqual(
            {
                key: request[key]
                for key in ("preset", "snapshot_id", "disk", "disk_id")
                if key in request
            },
            {"disk_id": disk_id},
        )
        self.assertEqual(request["labels"]["runtime-contract"], "baked-disk-image")
        self.assertLessEqual(set(request), supported_parameters)

    def test_copilot_runtime_uses_direct_sdk_session_contract(self) -> None:
        runtime_source = (
            SRC_ROOT / "agent_swarm_service" / "orchestration" / "copilot_runtime.py"
        ).read_text(encoding="utf-8")

        self.assertIn("from copilot import CopilotClient", runtime_source)
        self.assertIn("client.create_session", runtime_source)
        self.assertIn("session.send_and_wait", runtime_source)
        self.assertNotIn("inspect.signature", runtime_source)
        self.assertNotIn("from copilot_python import CopilotClient", runtime_source)
        self.assertNotIn("client.ask(", runtime_source)

    def test_worker_result_contract_rejects_retired_sandbox_output_fields(self) -> None:
        with self.assertRaises(ValidationError):
            WorkerExecutionResult.model_validate(
                {
                    "sandbox_id": "worker-1",
                    "summary": "Worker finished.",
                    "details": "Updated code.",
                    "branch_name": "swarm/octo/run-contract/integration",
                    "round_number": 1,
                    "head_commit_sha": "1" * 40,
                    "parent_commit_sha": "0" * 40,
                    "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                    "validation_summary": "Ran 1 validation command(s); 1 succeeded and 0 failed.",
                    "validation_results": [],
                    "files_touched": ["src/agent_swarm_service/orchestration/dts.py"],
                }
            )

        with self.assertRaises(ValidationError):
            WorkerExecutionResult.model_validate(
                {
                    "sandbox_id": "worker-1",
                    "summary": "Worker finished.",
                    "branch_name": "swarm/octo/run-contract/integration",
                    "head_commit_sha": "1" * 40,
                    "parent_commit_sha": "0" * 40,
                    "validation_summary": "Ran 1 validation command(s); 1 succeeded and 0 failed.",
                }
            )

    def test_worker_result_contract_exposes_branch_commit_and_validation_fields(self) -> None:
        fields = set(WorkerExecutionResult.model_fields)

        for required in (
            "branch_name",
            "head_commit_sha",
            "parent_commit_sha",
            "validation_summary",
            "changed_files",
            "no_changes",
        ):
            with self.subTest(field=required):
                self.assertIn(required, fields)
        self.assertNotIn("harvested_files", fields)

    def test_execution_round_establishes_review_head_for_all_no_change_workers_without_merge(self) -> None:
        run = _make_execution_round_run("run-worker-no-change")
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        worker_activity = _single_activity(worker_batch)
        worker_activity.complete(
            {
                "sandbox_id": "worker-no-change-1",
                "summary": "Worker confirmed the task was already satisfied.",
                "details": "No repository mutation was required.",
                "branch_name": "swarm/octo/run-worker-no-change/integration",
                "round_number": 1,
                "head_commit_sha": "a" * 40,
                "parent_commit_sha": "a" * 40,
                "changed_files": [],
                "validation_summary": "No validation commands were requested.",
                "validation_results": [],
                "no_changes": True,
            }
        )

        reviewer_activity = _resume_completed_task(generator, worker_batch)
        review_head_ready = SwarmRunState.model_validate(ctx.custom_statuses[-2])
        reviewing = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(reviewer_activity["name"], RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)
        self.assertEqual(review_head_ready.branch_state.current_head_sha, "a" * 40)
        self.assertEqual(reviewing.tasks[0].changed_files, [])
        self.assertEqual(reviewing.branch_state.current_head_sha, "a" * 40)
        self.assertIn("without new repo changes", review_head_ready.message)

    def test_execution_round_skips_no_change_worker_branches_during_fan_in(self) -> None:
        run = _make_execution_round_run(
            "run-mixed-worker-wave",
            tasks=[
                SwarmTaskState(
                    id="task-1",
                    title="Validate existing runtime slice",
                    status="Pending",
                    summary="Already done, just verify.",
                    history=[SwarmTaskStatusTransition(status="Pending")],
                ),
                SwarmTaskState(
                    id="task-2",
                    title="Implement remaining runtime slice",
                    status="Pending",
                    summary="Needs repo changes.",
                    history=[SwarmTaskStatusTransition(status="Pending")],
                ),
            ],
        )
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        worker_activities = _activity_group(worker_batch)
        self.assertEqual(len(worker_activities), 2)

        worker_activities[0].complete(
            {
                "sandbox_id": "worker-no-change-1",
                "summary": "Validated the task without repo changes.",
                "details": "The runtime slice was already present.",
                "branch_name": run.target_branch,
                "round_number": 1,
                "head_commit_sha": "a" * 40,
                "parent_commit_sha": "a" * 40,
                "changed_files": [],
                "validation_summary": "No validation commands were requested.",
                "validation_results": [],
                "no_changes": True,
            }
        )
        worker_activities[1].complete(
            {
                "sandbox_id": "worker-change-1",
                "summary": "Implemented the remaining runtime slice.",
                "details": "Added the missing repo changes.",
                "branch_name": "swarm/octo/run-mixed-worker-wave/task-2-r1",
                "round_number": 1,
                "head_commit_sha": "b" * 40,
                "parent_commit_sha": "a" * 40,
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "validation_summary": "pytest task-2 passed",
                "validation_results": [],
            }
        )

        merge_activity = _resume_completed_task(generator, worker_batch)
        self.assertEqual(merge_activity["name"], GIT_MERGE_ACTIVITY_NAME)
        self.assertEqual(
            [item["task_id"] for item in merge_activity["input"]["worker_branches"]],
            ["task-2"],
        )
        merge_activity.complete(
            {
                "sandbox_id": "merge-mixed-wave-1",
                "target_branch": run.target_branch,
                "head_commit_sha": "c" * 40,
                "parent_commit_sha": "a" * 40,
                "merged_branch_names": ["swarm/octo/run-mixed-worker-wave/task-2-r1"],
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "blocked": False,
                "blocked_reason": None,
            }
        )

        reviewer_activity = _resume_completed_task(generator, merge_activity)
        reviewing = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(reviewer_activity["name"], RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)
        self.assertEqual([task.status for task in reviewing.tasks], ["InReview", "InReview"])
        self.assertEqual(reviewing.tasks[0].changed_files, [])
        self.assertEqual(
            reviewing.tasks[1].changed_files,
            ["src/agent_swarm_service/orchestration/dts.py"],
        )
        self.assertEqual(reviewing.branch_state.current_head_sha, "c" * 40)

    def test_execution_round_tracks_commit_heads_from_worker_and_reviewer_results_before_publish(self) -> None:        
        run = _make_execution_round_run("run-real-head-progression")
        ctx = _FakeOrchestrationContext()
        generator = execution_round_orchestration(ctx, {"run": run.model_dump(mode="json"), "history": {}})

        worker_batch = next(generator)
        worker_activity = _single_activity(worker_batch)
        self.assertEqual(worker_activity["name"], RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME)

        worker_activity.complete(
            {
                "sandbox_id": "worker-commit-1",
                "summary": "Worker pushed the run branch head.",
                "details": "Recorded the real commit and validation output from the sandbox.",
                "branch_name": "swarm/octo/run-real-head-progression/task-1-r1",
                "round_number": 1,
                "head_commit_sha": "b" * 40,
                "parent_commit_sha": "a" * 40,
                "validation_summary": "pytest -q failed with 2 regressions",
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "validation_results": [],
            }
        )
        merge_activity = _resume_completed_task(generator, worker_batch)
        self.assertEqual(merge_activity["name"], GIT_MERGE_ACTIVITY_NAME)
        merge_activity.complete(
            {
                "sandbox_id": "merge-commit-1",
                "target_branch": "swarm/octo/run-real-head-progression/integration",
                "head_commit_sha": "c" * 40,
                "parent_commit_sha": "a" * 40,
                "merged_branch_names": ["swarm/octo/run-real-head-progression/task-1-r1"],
                "changed_files": ["src/agent_swarm_service/orchestration/dts.py"],
                "blocked": False,
                "blocked_reason": None,
            }
        )
        reviewer_activity = _resume_completed_task(generator, merge_activity)
        self.assertEqual(reviewer_activity["name"], RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME)

        reviewing = SwarmRunState.model_validate(ctx.custom_statuses[-1])
        self.assertEqual(reviewing.branch_state.current_head_sha, "c" * 40)
        self.assertIsNone(reviewing.branch_state.reviewed_head_sha)
        self.assertIsNone(reviewing.branch_state.approved_head_sha)

        reviewer_activity.complete(
            {
                "sandbox_id": "reviewer-commit-1",
                "outcome": "Approved",
                "summary": "Reviewer approved the pushed branch head.",
                "details": "The reviewed and approved heads should now match the real worker commit.",
                "findings": [],
                "fix_tasks": [],
                "replan_summary": None,
                "replan_findings": [],
                "target_branch": "swarm/octo/run-real-head-progression/integration",
                "pull_request_url": "https://github.com/octo/repo/compare/main...swarm/octo/run-real-head-progression/integration?expand=1",
            }
        )
        with self.assertRaises(StopIteration) as stop:
            _resume_completed_task(generator, reviewer_activity)

        approved = SwarmRunState.model_validate(stop.exception.value)
        self.assertEqual(approved.branch_state.current_head_sha, "c" * 40)
        self.assertEqual(approved.branch_state.reviewed_head_sha, "c" * 40)
        self.assertEqual(approved.branch_state.approved_head_sha, "c" * 40)
