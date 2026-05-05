from __future__ import annotations

import asyncio
import json
import logging
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from typing import Any, Literal, Protocol
from uuid import uuid4

from azure.identity import DefaultAzureCredential
from durabletask.azuremanaged.client import DurableTaskSchedulerClient
from durabletask.azuremanaged.worker import DurableTaskSchedulerWorker
from durabletask.client import OrchestrationState, OrchestrationStatus
from durabletask.task import RetryPolicy, when_all
from pydantic import BaseModel, Field

from agent_swarm_service.config import ServiceSettings
from agent_swarm_service.github.publishing import (
    GitHubPublishResult,
    GitHubPublisherProtocol,
)
from agent_swarm_service.orchestration.coordinator import SwarmRunCoordinator
from agent_swarm_service.orchestration.models import (
    CoordinatorCheckpoint,
    PlanFeedbackAction,
    PlanFeedbackSubmission,
    ReviewOutcome,
    RunOwner,
    SwarmActivitySummary,
    SwarmOptions,
    SwarmReviewFixTask,
    SwarmRunState,
    SwarmTaskState,
    SwarmTaskStatusTransition,
    utcnow,
)
from agent_swarm_service.orchestration.projections import build_projection_snapshot
from agent_swarm_service.orchestration.sandbox_execution import (
    AcaSandboxLifecycleExecutor,
    MergeBranchInput,
    MergeExecutionResult,
    MergeSandboxActivityInput,
    PlannerExecutionResult,
    PlannerSandboxActivityInput,
    ReviewerExecutionResult,
    WorkerExecutionResult,
    build_integration_branch_name,
    parse_repository_context,
    planned_task,
)
from agent_swarm_service.runtime.storage import RuntimeStorageBackendProtocol
from agent_swarm_service.sandboxes.aca_client import (
    attach_sandbox_context,
    sandbox_id,
)

logger = logging.getLogger(__name__)

SWARM_ORCHESTRATION_NAME = "SwarmOrchestration"
PLANNING_SUB_ORCHESTRATION_NAME = "PlanningSubOrchestration"
EXECUTION_ROUND_ORCHESTRATION_NAME = "ExecutionRoundOrchestration"
RUN_PLANNER_IN_SANDBOX_ACTIVITY_NAME = "RunPlannerInSandboxActivity"
RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME = "RunWorkerInSandboxActivity"
RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME = "RunReviewInSandboxActivity"
GIT_MERGE_ACTIVITY_NAME = "GitMergeActivity"
PUBLISH_TO_GITHUB_ACTIVITY_NAME = "PublishToGitHubActivity"
PLAN_REVIEW_EVENT_NAME = "PlanReview"
SWARM_ORCHESTRATOR_NAMES = (
    SWARM_ORCHESTRATION_NAME,
    PLANNING_SUB_ORCHESTRATION_NAME,
    EXECUTION_ROUND_ORCHESTRATION_NAME,
)
SWARM_ACTIVITY_NAMES = (
    RUN_PLANNER_IN_SANDBOX_ACTIVITY_NAME,
    RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME,
    RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME,
    GIT_MERGE_ACTIVITY_NAME,
    PUBLISH_TO_GITHUB_ACTIVITY_NAME,
)
_ACTIVE_RUNTIME_STATUSES = {
    OrchestrationStatus.PENDING,
    OrchestrationStatus.RUNNING,
    OrchestrationStatus.SUSPENDED,
}
_UNSET = object()
_SANDBOX_ROLE_RETRY_POLICY = RetryPolicy(
    first_retry_interval=timedelta(seconds=5),
    max_number_of_attempts=3,
    backoff_coefficient=2.0,
    max_retry_interval=timedelta(seconds=30),
)


@dataclass(frozen=True)
class SwarmWorkerRegistration:
    orchestrators: tuple[str, ...] = SWARM_ORCHESTRATOR_NAMES
    activities: tuple[str, ...] = SWARM_ACTIVITY_NAMES


@dataclass(frozen=True)
class SwarmHistoryWindow:
    total_execution_rounds: int = 0
    rounds_since_continue_as_new: int = 0
    title: str | None = None
    publish_status: str | None = None
    task_ids: tuple[str, ...] = ()
    pending_replan_summary: str | None = None
    pending_replan_findings: tuple[str, ...] = ()

    def record_round(self) -> "SwarmHistoryWindow":
        return SwarmHistoryWindow(
            total_execution_rounds=self.total_execution_rounds + 1,
            rounds_since_continue_as_new=self.rounds_since_continue_as_new + 1,
            title=self.title,
            publish_status=self.publish_status,
            task_ids=self.task_ids,
            pending_replan_summary=self.pending_replan_summary,
            pending_replan_findings=self.pending_replan_findings,
        )

    def should_continue_as_new(self, max_rounds_before_continue_as_new: int | None) -> bool:
        return (
            max_rounds_before_continue_as_new is not None
            and max_rounds_before_continue_as_new > 0
            and self.rounds_since_continue_as_new >= max_rounds_before_continue_as_new
        )

    def continue_as_new(self) -> "SwarmHistoryWindow":
        return SwarmHistoryWindow(
            total_execution_rounds=self.total_execution_rounds,
            rounds_since_continue_as_new=0,
            title=self.title,
            publish_status=self.publish_status,
            task_ids=self.task_ids,
            pending_replan_summary=self.pending_replan_summary,
            pending_replan_findings=self.pending_replan_findings,
        )


class SwarmRuntimeEnvelope(BaseModel):
    run: SwarmRunState
    history: SwarmHistoryWindow = Field(default_factory=SwarmHistoryWindow)


SandboxRoleName = Literal["planner", "worker", "reviewer", "merge"]


class SandboxRoleActivityInput(BaseModel):
    role: SandboxRoleName
    run: SwarmRunState
    task: SwarmTaskState | None = None


class SandboxExecutionActivityInput(BaseModel):
    run: SwarmRunState
    sandbox: dict[str, Any]
    task: SwarmTaskState | None = None


class PublishActivityInput(BaseModel):
    run: SwarmRunState


def build_worker_registration() -> SwarmWorkerRegistration:
    return SwarmWorkerRegistration()


def planning_instance_id(owner_session_id: str, run_id: str) -> str:
    return f"swarm-planning:{owner_session_id}:{run_id}"


def replan_instance_id(owner_session_id: str, run_id: str, replan_count: int) -> str:
    return f"swarm-replan:{owner_session_id}:{run_id}:{replan_count}"


def execution_round_instance_id(owner_session_id: str, run_id: str, round_number: int) -> str:
    return f"swarm-round:{owner_session_id}:{run_id}:{round_number}"


@dataclass(frozen=True)
class DtsConnectionInfo:
    host_address: str
    taskhub: str
    secure_channel: bool
    client_id: str | None

    @classmethod
    def from_connection_string(cls, connection_string: str) -> "DtsConnectionInfo":
        parts: dict[str, str] = {}
        for item in connection_string.split(";"):
            if "=" not in item:
                continue
            key, value = item.split("=", 1)
            key = key.strip().lower()
            value = value.strip()
            if key and value:
                parts[key] = value
        endpoint = parts.get("endpoint")
        taskhub = parts.get("taskhub")
        if not endpoint or not taskhub:
            raise ValueError("DTS_CONNECTION_STRING must include Endpoint and TaskHub.")
        secure_channel = not endpoint.lower().startswith("http://")
        return cls(
            host_address=endpoint,
            taskhub=taskhub,
            secure_channel=secure_channel,
            client_id=parts.get("clientid"),
        )


class RunOwnershipStore(Protocol):
    async def add(self, run: SwarmRunState) -> None: ...

    async def remove(self, owner_session_id: str, run_id: str) -> None: ...

    async def list_run_ids(self, owner_session_id: str) -> list[str]: ...

    async def list_all_run_ids(self) -> list[str]: ...


class DurableRunOwnershipStore:
    def __init__(self, backend: RuntimeStorageBackendProtocol) -> None:
        self._backend = backend

    async def add(self, run: SwarmRunState) -> None:
        await self._backend.write_json(
            self._path(run.owner.user_id, run.id),
            {
                "run_id": run.id,
                "owner_user_id": run.owner.user_id,
                "owner_login": run.owner.login,
                "created_at_utc": run.created_at_utc.isoformat(),
            },
        )

    async def remove(self, owner_session_id: str, run_id: str) -> None:
        await self._backend.delete(self._path(owner_session_id, run_id))

    async def list_run_ids(self, owner_session_id: str) -> list[str]:
        records = await self._backend.list_json(self._prefix(owner_session_id))
        run_ids: list[str] = []
        for _, payload in records:
            run_id = payload.get("run_id")
            if isinstance(run_id, str) and run_id:
                run_ids.append(run_id)
        return sorted(set(run_ids))

    async def list_all_run_ids(self) -> list[str]:
        records = await self._backend.list_json("run-ownership/")
        run_ids: list[str] = []
        for _, payload in records:
            run_id = payload.get("run_id")
            if isinstance(run_id, str) and run_id:
                run_ids.append(run_id)
        return sorted(set(run_ids))

    @staticmethod
    def _prefix(owner_session_id: str) -> str:
        return f"run-ownership/{owner_session_id}/"

    @classmethod
    def _path(cls, owner_session_id: str, run_id: str) -> str:
        return f"{cls._prefix(owner_session_id)}{run_id}.json"


def _run_async(awaitable):
    return asyncio.run(awaitable)


def _coerce_utc_timestamp(value: datetime) -> datetime:
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


def _runtime_input(run: SwarmRunState, history: SwarmHistoryWindow | None = None) -> dict[str, Any]:
    return _compact_model_dump(
        SwarmRuntimeEnvelope(
            run=run,
            history=history or SwarmHistoryWindow(),
        )
    )


def _compact_model_dump(model: BaseModel) -> dict[str, Any]:
    return model.model_dump(mode="json", exclude_none=True, exclude_defaults=True)


def _load_runtime_envelope(input: dict[str, Any]) -> SwarmRuntimeEnvelope:
    if "run" in input:
        return SwarmRuntimeEnvelope.model_validate(input)
    return SwarmRuntimeEnvelope(run=SwarmRunState.model_validate(input))


def _extract_run_payload(payload: dict[str, Any]) -> dict[str, Any]:
    if "run" in payload and isinstance(payload["run"], dict):
        return payload["run"]
    return payload


def _default_target_branch(run: SwarmRunState) -> str:
    repo = parse_repository_context(run.repository_url, run.base_branch)
    return run.target_branch or build_integration_branch_name(run, repo)


def _ensure_branch_tracking(run: SwarmRunState) -> SwarmRunState:
    branch_name = _default_target_branch(run)
    branch_state = run.branch_state.model_copy(
        update={
            "branch_name": branch_name,
        }
    )
    return run.model_copy(update={"target_branch": branch_name, "branch_state": branch_state})


def _mark_execution_state(run: SwarmRunState, *, command_type: str, worker_id: str) -> SwarmRunState:
    now = utcnow()
    return run.model_copy(
        update={
            "execution": run.execution.model_copy(
                update={
                    "owner_id": worker_id,
                    "last_command_type": command_type,
                    "acquired_at_utc": run.execution.acquired_at_utc or now,
                    "heartbeat_at_utc": now,
                    "last_progress_at_utc": now,
                    "attempt_count": run.execution.attempt_count + 1,
                }
            )
        }
    )


def _next_checkpoint(run: SwarmRunState, *, phase: str, status: str) -> CoordinatorCheckpoint:
    return CoordinatorCheckpoint(
        run_id=run.id,
        phase=phase,
        status=status,
        sequence=(run.checkpoint.sequence + 1) if run.checkpoint else 1,
    )


def _advance_run(
    run: SwarmRunState,
    *,
    runtime_status: str | None = None,
    status: str,
    phase: str,
    message: str,
) -> SwarmRunState:
    return run.model_copy(
        update={
            "runtime_status": runtime_status or run.runtime_status,
            "status": status,
            "phase": phase,
            "message": message,
            "last_updated_at_utc": utcnow(),
            "checkpoint": _next_checkpoint(run, phase=phase, status=status),
        }
    )


def _current_head_checkpoint(run: SwarmRunState) -> int | None:
    return run.branch_state.current_head_checkpoint_sequence


def _record_branch_head(
    run: SwarmRunState,
    *,
    branch_name: str,
    commit_sha: str,
) -> SwarmRunState:
    next_sequence = (run.checkpoint.sequence + 1) if run.checkpoint else 1
    return run.model_copy(
        update={
            "target_branch": branch_name,
            "branch_state": run.branch_state.model_copy(
                update={
                    "branch_name": branch_name,
                    "current_head_checkpoint_sequence": next_sequence,
                    "current_head_sha": commit_sha,
                    "merge_status": "PendingReview",
                    "merge_resolution_state": "NotRequired",
                    "merge_resolution_sandbox_id": None,
                }
            ),
        }
    )


def _apply_worker_completion_result(
    run: SwarmRunState,
    *,
    updated_tasks: list[SwarmTaskState],
) -> SwarmRunState:
    updated_run = run.model_copy(
        update={
            "tasks": updated_tasks,
            "plan": run.plan.model_copy(update={"tasks": updated_tasks}),
        }
    )
    integration_branch = run.target_branch or _default_target_branch(run)
    return updated_run.model_copy(
        update={
            "branch_state": updated_run.branch_state.model_copy(
                update={
                    "branch_name": integration_branch,
                    "merge_status": "PendingReview",
                    "merge_resolution_state": "NotRequired",
                    "merge_resolution_sandbox_id": None,
                }
            )
        }
    )


def _record_reviewed_head(
    run: SwarmRunState,
    *,
    approved: bool,
    next_wave: int | None = None,
    reset_wave_round: bool = False,
) -> SwarmRunState:
    branch_name = _default_target_branch(run)
    reviewed_checkpoint = _current_head_checkpoint(run)
    branch_updates: dict[str, Any] = {
        "branch_name": branch_name,
        "reviewed_checkpoint_sequence": reviewed_checkpoint,
        "reviewed_head_sha": run.branch_state.current_head_sha,
        "current_wave_round": 0 if reset_wave_round else run.branch_state.current_wave_round + 1,
        "merge_resolution_state": "NotRequired",
        "merge_resolution_sandbox_id": None,
    }
    if next_wave is not None:
        branch_updates["active_wave"] = next_wave
    if approved:
        branch_updates["approved_branch_name"] = branch_name
        branch_updates["approved_checkpoint_sequence"] = reviewed_checkpoint
        branch_updates["approved_head_sha"] = run.branch_state.current_head_sha
        branch_updates["merge_status"] = "Approved"
    return run.model_copy(
        update={
            "target_branch": branch_name,
            "branch_state": run.branch_state.model_copy(update=branch_updates),
        }
    )


def _replace_last_activity(
    activities: list[SwarmActivitySummary],
    *,
    activity_id: str | None = None,
    status: str,
    summary: str | None,
    details: str | None = None,
    branch_name: str | None = None,
    round_number: int | None = None,
    active_sandbox_id: str | None | object = _UNSET,
    findings: list | None = None,
    fix_tasks: list | None = None,
    replan_summary: str | None | object = _UNSET,
    replan_findings: list[str] | None = None,
    publish_status: str | None | object = _UNSET,
    publish_error: str | None | object = _UNSET,
    pull_request_url: str | None | object = _UNSET,
    pull_request_number: int | None | object = _UNSET,
    head_commit_sha: str | None | object = _UNSET,
    parent_commit_sha: str | None | object = _UNSET,
    changed_files: list[str] | None = None,
    validation_summary: str | None | object = _UNSET,
    validation_results: list | None = None,
) -> list[SwarmActivitySummary]:
    if not activities:
        return activities
    target_index = len(activities) - 1
    if activity_id is not None:
        for index, activity in enumerate(activities):
            if activity.id == activity_id:
                target_index = index
                break
        else:
            return activities
    target = activities[target_index]
    updated = target.model_copy(
        update={
            "status": status,
            "summary": summary,
            "details": details if details is not None else target.details,
            "branch_name": branch_name if branch_name is not None else target.branch_name,
            "round_number": round_number if round_number is not None else target.round_number,
            "active_sandbox_id": target.active_sandbox_id if active_sandbox_id is _UNSET else active_sandbox_id,
            "findings": findings if findings is not None else target.findings,
            "fix_tasks": fix_tasks if fix_tasks is not None else target.fix_tasks,
            "replan_summary": target.replan_summary if replan_summary is _UNSET else replan_summary,
            "replan_findings": replan_findings if replan_findings is not None else target.replan_findings,
            "publish_status": target.publish_status if publish_status is _UNSET else publish_status,
            "publish_error": target.publish_error if publish_error is _UNSET else publish_error,
            "pull_request_url": target.pull_request_url if pull_request_url is _UNSET else pull_request_url,
            "pull_request_number": (
                target.pull_request_number
                if pull_request_number is _UNSET
                else pull_request_number
            ),
            "head_commit_sha": target.head_commit_sha if head_commit_sha is _UNSET else head_commit_sha,
            "parent_commit_sha": (
                target.parent_commit_sha if parent_commit_sha is _UNSET else parent_commit_sha
            ),
            "changed_files": changed_files if changed_files is not None else target.changed_files,
            "validation_summary": (
                target.validation_summary if validation_summary is _UNSET else validation_summary
            ),
            "validation_results": (
                validation_results if validation_results is not None else target.validation_results
            ),
        }
    )
    return [*activities[:target_index], updated, *activities[target_index + 1 :]]


def _task_exception_message(task: Any, default: str) -> str:
    if not getattr(task, "is_failed", False):
        return default
    try:
        task_exception = task.get_exception()
    except Exception:
        return default
    if task_exception is None:
        return default
    message = str(task_exception)
    return message or default


def _next_ready_task_index(run: SwarmRunState) -> int | None:
    ready_indices = _all_ready_task_indices(run)
    return ready_indices[0] if ready_indices else None


def _all_ready_task_indices(run: SwarmRunState) -> list[int]:
    completed = {task.id for task in run.tasks if task.is_completed}
    ready_indices: list[int] = []
    for index, task in enumerate(run.tasks):
        if task.is_completed or task.status not in {"Pending", "Planned"}:
            continue
        if all(dependency in completed for dependency in task.dependencies):
            ready_indices.append(index)
    return ready_indices


def _pending_review_task_indices(run: SwarmRunState) -> list[int]:
    return [index for index, task in enumerate(run.tasks) if task.status == "PendingReview"]


def _completed_worker_branches_in_task_order(
    run: SwarmRunState,
    *,
    task_indices: list[int],
) -> list[MergeBranchInput]:
    branch_inputs: list[MergeBranchInput] = []
    seen: set[tuple[str, str]] = set()
    for task_index in task_indices:
        task = run.tasks[task_index]
        branch_name = (task.branch_name or "").strip()
        head_commit_sha = (task.head_commit_sha or "").strip()
        if not branch_name or not head_commit_sha:
            continue
        if task.parent_commit_sha == task.head_commit_sha and not task.changed_files:
            continue
        key = (branch_name, head_commit_sha)
        if key in seen:
            continue
        seen.add(key)
        branch_inputs.append(
            MergeBranchInput(
                task_id=task.id,
                branch_name=branch_name,
                head_commit_sha=head_commit_sha,
                parent_commit_sha=task.parent_commit_sha,
                round_number=task.round_number,
                changed_files=task.changed_files,
                no_changes=task.parent_commit_sha == task.head_commit_sha and not task.changed_files,
            )
        )
    return branch_inputs


def _establish_review_head_from_no_change_tasks(
    run: SwarmRunState,
    *,
    task_indices: list[int],
) -> SwarmRunState:
    if run.branch_state.current_head_sha:
        return run
    candidate_shas: list[str] = []
    for task_index in task_indices:
        task = run.tasks[task_index]
        head_commit_sha = (task.head_commit_sha or "").strip()
        if not head_commit_sha:
            continue
        if task.parent_commit_sha != task.head_commit_sha or task.changed_files:
            return run
        candidate_shas.append(head_commit_sha)
    if not candidate_shas:
        return run
    review_head_sha = candidate_shas[0]
    if any(candidate != review_head_sha for candidate in candidate_shas[1:]):
        raise ValueError(
            "No-change worker tasks did not agree on the integration head commit required for reviewer validation."
        )
    return _record_branch_head(
        run,
        branch_name=run.target_branch or _default_target_branch(run),
        commit_sha=review_head_sha,
    )


def _apply_plan_feedback(run: SwarmRunState, feedback: PlanFeedbackSubmission) -> SwarmRunState:
    revised_tasks = [
        planned_task(item.id, item.title, item.description)
        for item in feedback.revised_tasks
    ]
    updates: dict[str, Any] = {
        "awaiting_plan_review": False,
        "plan_feedback_history": [*run.plan_feedback_history, feedback],
        "runtime_status": "Pending",
        "status": "Queued",
        "phase": "Queued",
        "message": "Plan feedback captured for DTS orchestration pickup.",
        "last_updated_at_utc": utcnow(),
        "checkpoint": _next_checkpoint(run, phase="Queued", status="Queued"),
        "intent": run.intent.model_copy(update={"resume_requested": False}),
    }
    if revised_tasks:
        updates["tasks"] = revised_tasks
        updates["plan"] = run.plan.model_copy(update={"tasks": revised_tasks, "design_document": None})
    elif feedback.action is PlanFeedbackAction.REQUEST_CHANGES:
        updates["plan"] = run.plan.model_copy(update={"design_document": None})
    return run.model_copy(update=updates)


def _current_planning_instance_id(run: SwarmRunState) -> str:
    if run.replan_count > 0 and not run.plan.design_document:
        return replan_instance_id(run.owner.user_id, run.id, run.replan_count)
    return planning_instance_id(run.owner.user_id, run.id)


def _append_publish_details(details: str | None, publish_result: GitHubPublishResult) -> str | None:
    lines = [details] if details else []
    if publish_result.status == "Published":
        lines.append(
            f"GitHub publish created PR #{publish_result.pull_request_number} from '{publish_result.target_branch}'."
        )
    elif publish_result.status == "Skipped":
        lines.append(f"GitHub publish was skipped: {publish_result.error_message}")
    elif publish_result.status == "Failed":
        lines.append(f"GitHub publish failed: {publish_result.error_message}")
    return "\n\n".join(line for line in lines if line)


def _build_publish_completion_message(publish_result: GitHubPublishResult) -> str:
    if publish_result.status == "Published":
        return (
            f"Reviewer approved the run and GitHub publish created PR #{publish_result.pull_request_number} "
            f"from '{publish_result.target_branch}'."
        )
    if publish_result.status == "Skipped":
        return f"Reviewer approved the run, but GitHub publish was skipped: {publish_result.error_message}"
    if publish_result.status == "Failed":
        return f"Reviewer approved the run, but GitHub publish failed: {publish_result.error_message}"
    return "Reviewer approved the completed run."


def _build_review_completion_summary(run: SwarmRunState, publish_result: GitHubPublishResult) -> str:
    if publish_result.status == "Published":
        return f"Reviewer approved and published the execution wave for {run.repository_url}."
    if publish_result.status == "Skipped":
        return f"Reviewer approved the execution wave for {run.repository_url}, but GitHub publish was skipped."
    if publish_result.status == "Failed":
        return f"Reviewer approved the execution wave for {run.repository_url}, but GitHub publish failed."
    return "Reviewer approved the completed run."


def _apply_publish_result(run: SwarmRunState, publish_result: GitHubPublishResult) -> SwarmRunState:
    branch_name = run.target_branch or run.branch_state.branch_name or publish_result.target_branch or _default_target_branch(run)
    current_head_sha = run.branch_state.current_head_sha or publish_result.commit_sha
    reviewed_head_sha = run.branch_state.reviewed_head_sha or current_head_sha
    approved_head_sha = run.branch_state.approved_head_sha or reviewed_head_sha
    normalized_publish_result = publish_result.model_copy(
        update={
            "target_branch": branch_name,
            "commit_sha": current_head_sha,
        }
    )
    completed = run.model_copy(
        update={
            "publish_status": normalized_publish_result.status,
            "publish_error": normalized_publish_result.error_message,
            "target_branch": branch_name,
            "branch_state": run.branch_state.model_copy(
                update={
                    "branch_name": branch_name,
                    "current_head_sha": current_head_sha,
                    "reviewed_head_sha": reviewed_head_sha,
                    "approved_branch_name": (
                        run.branch_state.approved_branch_name
                        or branch_name
                    ),
                    "approved_head_sha": approved_head_sha,
                    "merge_status": "Published" if normalized_publish_result.status == "Published" else "Approved",
                    "merge_resolution_state": "NotRequired",
                    "merge_resolution_sandbox_id": None,
                }
            ),
            "pull_request_url": normalized_publish_result.pull_request_url,
            "pull_request_number": normalized_publish_result.pull_request_number,
            "reviewer_summaries": _replace_last_activity(
                run.reviewer_summaries,
                status="completed",
                summary=_build_review_completion_summary(run, normalized_publish_result),
                details=_append_publish_details(
                    run.reviewer_summaries[-1].details if run.reviewer_summaries else None,
                    normalized_publish_result,
                ),
                publish_status=normalized_publish_result.status,
                publish_error=normalized_publish_result.error_message,
                pull_request_url=normalized_publish_result.pull_request_url,
                pull_request_number=normalized_publish_result.pull_request_number,
            ),
        }
    )
    return _advance_run(
        completed,
        runtime_status="Completed",
        status="Completed",
        phase="Completed",
        message=_build_publish_completion_message(normalized_publish_result),
    )


def _derive_title(prompt: str) -> str:
    normalized = " ".join(prompt.split())
    return normalized[:77] + "..." if len(normalized) > 80 else normalized


def _prefer_sub_orchestration_run(
    base_run: SwarmRunState,
    base_state: OrchestrationState,
    candidate_state: OrchestrationState | None,
) -> tuple[SwarmRunState, OrchestrationState]:
    if candidate_state is None:
        return base_run, base_state
    candidate_run = _hydrate_run_state(candidate_state)
    if candidate_run is None or candidate_run.owner.user_id != base_run.owner.user_id:
        return base_run, base_state
    if (
        base_state.runtime_status is OrchestrationStatus.FAILED
        and candidate_state.runtime_status is OrchestrationStatus.FAILED
        and (candidate_run.checkpoint.sequence if candidate_run.checkpoint else -1)
        >= (base_run.checkpoint.sequence if base_run.checkpoint else -1)
    ):
        return candidate_run, candidate_state
    if candidate_state.runtime_status in _ACTIVE_RUNTIME_STATUSES and base_state.runtime_status not in _ACTIVE_RUNTIME_STATUSES:
        return candidate_run, candidate_state
    if candidate_state.runtime_status in _ACTIVE_RUNTIME_STATUSES and candidate_run.last_updated_at_utc >= base_run.last_updated_at_utc:
        return candidate_run, candidate_state
    if candidate_run.last_updated_at_utc > base_run.last_updated_at_utc:
        return candidate_run, candidate_state
    return base_run, base_state


def _make_run_planner_activity(
    sandbox_lifecycle: AcaSandboxLifecycleExecutor,
    sandbox_client: Any,
):
    def run_planner_in_sandbox_activity(activity_ctx, input: dict[str, Any]) -> dict[str, Any]:
        if "sandbox" in input:
            payload = PlannerSandboxActivityInput.model_validate(input)
            result = _run_async(sandbox_lifecycle.execute_planner(payload))
        else:
            payload = SandboxRoleActivityInput.model_validate({**input, "role": "planner"})
            result = _run_sandbox_role_activity(
                sandbox_lifecycle=sandbox_lifecycle,
                sandbox_client=sandbox_client,
                payload=payload,
                activity_ctx=activity_ctx,
                activity_name=RUN_PLANNER_IN_SANDBOX_ACTIVITY_NAME,
            )
        return result.model_dump(mode="json")

    run_planner_in_sandbox_activity.__name__ = RUN_PLANNER_IN_SANDBOX_ACTIVITY_NAME
    return run_planner_in_sandbox_activity


def _make_run_worker_activity(
    sandbox_lifecycle: AcaSandboxLifecycleExecutor,
    sandbox_client: Any,
):
    def run_worker_in_sandbox_activity(activity_ctx, input: dict[str, Any]) -> dict[str, Any]:
        if "sandbox" in input:
            payload = SandboxExecutionActivityInput.model_validate(input)
            task = _require_worker_task(payload.task)
            result = _run_async(sandbox_lifecycle.execute_worker(payload.run, task, payload.sandbox))
        else:
            payload = SandboxRoleActivityInput.model_validate({**input, "role": "worker"})
            result = _run_sandbox_role_activity(
                sandbox_lifecycle=sandbox_lifecycle,
                sandbox_client=sandbox_client,
                payload=payload,
                activity_ctx=activity_ctx,
                activity_name=RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME,
            )
        return result.model_dump(mode="json")

    run_worker_in_sandbox_activity.__name__ = RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME
    return run_worker_in_sandbox_activity


def _make_run_review_activity(
    sandbox_lifecycle: AcaSandboxLifecycleExecutor,
    sandbox_client: Any,
):
    def run_review_in_sandbox_activity(activity_ctx, input: dict[str, Any]) -> dict[str, Any]:
        if "sandbox" in input:
            payload = SandboxExecutionActivityInput.model_validate(input)
            result = _run_async(sandbox_lifecycle.execute_reviewer(payload.run, payload.sandbox))
        else:
            payload = SandboxRoleActivityInput.model_validate({**input, "role": "reviewer"})
            result = _run_sandbox_role_activity(
                sandbox_lifecycle=sandbox_lifecycle,
                sandbox_client=sandbox_client,
                payload=payload,
                activity_ctx=activity_ctx,
                activity_name=RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME,
            )
        return result.model_dump(mode="json")

    run_review_in_sandbox_activity.__name__ = RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME
    return run_review_in_sandbox_activity


def _make_git_merge_activity(
    sandbox_lifecycle: AcaSandboxLifecycleExecutor,
    sandbox_client: Any,
):
    def git_merge_activity(activity_ctx, input: dict[str, Any]) -> dict[str, Any]:
        payload = MergeSandboxActivityInput.model_validate(input)
        result = _run_sandbox_role_activity(
            sandbox_lifecycle=sandbox_lifecycle,
            sandbox_client=sandbox_client,
            payload=SandboxRoleActivityInput(role="merge", run=payload.run),
            activity_ctx=activity_ctx,
            activity_name=GIT_MERGE_ACTIVITY_NAME,
            merge_branches=payload.worker_branches,
        )
        return result.model_dump(mode="json")

    git_merge_activity.__name__ = GIT_MERGE_ACTIVITY_NAME
    return git_merge_activity


def _make_publish_activity(publish_service: GitHubPublisherProtocol):
    def publish_to_github_activity(_, input: dict[str, Any]) -> dict[str, Any]:
        payload = PublishActivityInput.model_validate(input)
        run = payload.run
        repo = parse_repository_context(run.repository_url, run.base_branch)
        target_branch = run.target_branch or build_integration_branch_name(run, repo)
        result = _run_async(
            publish_service.publish_run(
                run,
                repo=repo,
                target_branch=target_branch,
            )
        )
        return result.model_dump(mode="json")

    publish_to_github_activity.__name__ = PUBLISH_TO_GITHUB_ACTIVITY_NAME
    return publish_to_github_activity


def _require_worker_task(task: SwarmTaskState | None) -> SwarmTaskState:
    if task is None:
        raise ValueError("Worker activity requires a task payload.")
    return task


def _create_role_sandbox(
    *,
    sandbox_lifecycle: AcaSandboxLifecycleExecutor,
    sandbox_client: Any,
    payload: SandboxRoleActivityInput,
) -> dict[str, Any]:
    request = sandbox_lifecycle.build_sandbox_request(payload.role, payload.run, payload.task)
    request["environment"].update(_run_async(sandbox_lifecycle.build_execution_environment(payload.run)))
    sandbox_group = str(request.pop("sandbox_group"))
    resource_group = request.pop("resource_group", None)
    raw = _run_async(
        asyncio.to_thread(
            sandbox_client.create_sandbox,
            sandbox_group,
            resource_group=resource_group,
            **request,
        )
    )
    return attach_sandbox_context(
        raw,
        sandbox_group=sandbox_group,
        resource_group=resource_group,
        default_resource_group=sandbox_lifecycle._settings.azure.resource_group,
    )


def _execute_sandbox_role(
    *,
    sandbox_lifecycle: AcaSandboxLifecycleExecutor,
    payload: SandboxRoleActivityInput,
    sandbox: dict[str, Any],
) -> PlannerExecutionResult | WorkerExecutionResult | ReviewerExecutionResult | MergeExecutionResult:
    if payload.role == "planner":
        return _run_async(sandbox_lifecycle.execute_planner(payload.run, sandbox))
    if payload.role == "worker":
        task = _require_worker_task(payload.task)
        return _run_async(sandbox_lifecycle.execute_worker(payload.run, task, sandbox))
    if payload.role == "merge":
        raise ValueError("Merge role execution requires worker branch payload.")
    return _run_async(sandbox_lifecycle.execute_reviewer(payload.run, sandbox))


def _run_sandbox_role_activity(
    *,
    sandbox_lifecycle: AcaSandboxLifecycleExecutor,
    sandbox_client: Any,
    payload: SandboxRoleActivityInput,
    activity_ctx: Any | None = None,
    activity_name: str,
    merge_branches: list[MergeBranchInput] | None = None,
) -> PlannerExecutionResult | WorkerExecutionResult | ReviewerExecutionResult | MergeExecutionResult:
    if payload.role == "worker":
        _require_worker_task(payload.task)
    worker_branch_count = len(merge_branches) if merge_branches is not None else None
    execution_id = uuid4().hex
    logger.info(
        "Starting sandbox role activity %s",
        _activity_execution_log_context(
            activity_name=activity_name,
            run_id=payload.run.id,
            role=payload.role,
            task_id=payload.task.id if payload.task is not None else None,
            activity_ctx=activity_ctx,
            execution_id=execution_id,
            worker_branch_count=worker_branch_count,
        ),
    )
    sandbox = _create_role_sandbox(
        sandbox_lifecycle=sandbox_lifecycle,
        sandbox_client=sandbox_client,
        payload=payload,
    )
    logger.info(
        "Created sandbox for role activity %s",
        _activity_execution_log_context(
            activity_name=activity_name,
            run_id=payload.run.id,
            role=payload.role,
            task_id=payload.task.id if payload.task is not None else None,
            activity_ctx=activity_ctx,
            execution_id=execution_id,
            sandbox=sandbox,
            worker_branch_count=worker_branch_count,
        ),
    )
    failed = False
    try:
        if payload.role == "merge":
            if merge_branches is None:
                raise ValueError("Merge role activity requires worker branch payload.")
            result = _run_async(
                sandbox_lifecycle.execute_merge(payload.run, merge_branches, sandbox)
            )
        else:
            result = _execute_sandbox_role(
                sandbox_lifecycle=sandbox_lifecycle,
                payload=payload,
                sandbox=sandbox,
            )
        logger.info(
            "Completed sandbox role activity %s",
            _activity_execution_log_context(
                activity_name=activity_name,
                run_id=payload.run.id,
                role=payload.role,
                task_id=payload.task.id if payload.task is not None else None,
                activity_ctx=activity_ctx,
                execution_id=execution_id,
                sandbox=sandbox,
                worker_branch_count=worker_branch_count,
            ),
        )
        return result
    except Exception:
        failed = True
        logger.exception(
            "Sandbox role activity failed %s",
            _activity_execution_log_context(
                activity_name=activity_name,
                run_id=payload.run.id,
                role=payload.role,
                task_id=payload.task.id if payload.task is not None else None,
                activity_ctx=activity_ctx,
                execution_id=execution_id,
                sandbox=sandbox,
                worker_branch_count=worker_branch_count,
            ),
        )
        raise
    finally:
        _cleanup_sandboxes(sandbox_lifecycle, [sandbox], failed=failed)


def _cleanup_sandboxes(
    sandbox_lifecycle: AcaSandboxLifecycleExecutor,
    sandboxes: list[dict[str, Any]],
    *,
    failed: bool = False,
) -> None:
    for sandbox in sandboxes:
        _run_async(sandbox_lifecycle.cleanup_sandbox(sandbox, failed=failed))


def _activity_execution_log_context(
    *,
    activity_name: str,
    run_id: str,
    role: str | None = None,
    task_id: str | None = None,
    activity_ctx: Any | None = None,
    execution_id: str | None = None,
    sandbox: dict[str, Any] | None = None,
    worker_branch_count: int | None = None,
) -> str:
    payload: dict[str, Any] = {
        "activity": activity_name,
        "run_id": run_id,
    }
    if role is not None:
        payload["role"] = role
    if task_id is not None:
        payload["task_id"] = task_id
    if execution_id is not None:
        payload["execution_id"] = execution_id
    if worker_branch_count is not None:
        payload["worker_branch_count"] = worker_branch_count
    if activity_ctx is not None:
        orchestration_id = getattr(activity_ctx, "orchestration_id", None)
        activity_task_id = getattr(activity_ctx, "task_id", None)
        if orchestration_id is not None:
            payload["orchestration_id"] = orchestration_id
        if activity_task_id is not None:
            payload["activity_task_id"] = activity_task_id
    if sandbox is not None:
        payload["sandbox_id"] = sandbox_id(sandbox)
        payload["sandbox_group"] = sandbox.get("sandbox_group")
        payload["resource_group"] = sandbox.get("resource_group")
    return json.dumps(payload, sort_keys=True)


def planning_sub_orchestration(ctx, input: dict[str, Any]) -> dict[str, Any]:
    run = _ensure_branch_tracking(_load_runtime_envelope(input).run)
    while True:
        planner_summary = SwarmActivitySummary(
            id=f"planner-{run.checkpoint.sequence + 1 if run.checkpoint else 1}",
            kind="planner",
            title="Create execution plan",
            status="running",
            summary="Planner sandbox is preparing the execution plan.",
        )
        run = _advance_run(
            run.model_copy(
                update={
                    "active_planner_sandbox_id": None,
                    "branch_state": run.branch_state.model_copy(update={"merge_status": "Planning"}),
                    "planner_summaries": [*run.planner_summaries, planner_summary],
                    "intent": run.intent.model_copy(update={"resume_requested": False}),
                }
            ),
            runtime_status="Running",
            status="Running",
            phase="Planning",
            message="Planner sandbox is generating the plan.",
        )
        ctx.set_custom_status(run.model_dump(mode="json"))
        try:
            result_payload = yield ctx.call_activity(
                RUN_PLANNER_IN_SANDBOX_ACTIVITY_NAME,
                input=_compact_model_dump(SandboxRoleActivityInput(role="planner", run=run)),
                retry_policy=_SANDBOX_ROLE_RETRY_POLICY,
            )
        except Exception as exc:
            failed = _advance_run(
                run.model_copy(
                    update={
                        "active_planner_sandbox_id": None,
                        "failure_message": str(exc),
                        "planner_summaries": _replace_last_activity(
                            run.planner_summaries,
                            status="failed",
                            summary="Planner sandbox failed.",
                            details=str(exc),
                            active_sandbox_id=None,
                        ),
                    }
                ),
                runtime_status="Failed",
                status="Failed",
                phase="Failed",
                message="Planner sandbox failed.",
            )
            ctx.set_custom_status(failed.model_dump(mode="json"))
            raise
        result = PlannerExecutionResult.model_validate(result_payload)
        plan = result.to_plan()
        awaiting_plan_review = run.options.planning.human_review_mode.value == "Required"
        planned = run.model_copy(
            update={
                "plan": plan,
                "tasks": plan.tasks,
                "active_planner_sandbox_id": None,
                "awaiting_plan_review": awaiting_plan_review,
                "pending_replan_summary": None,
                "pending_replan_findings": [],
                "branch_state": run.branch_state.model_copy(
                    update={"merge_status": "PlanReview" if awaiting_plan_review else "Executing"}
                ),
                "planner_summaries": _replace_last_activity(
                    run.planner_summaries,
                    status="completed",
                    summary=result.summary,
                    details=result.design_document,
                    active_sandbox_id=None,
                ),
            }
        )
        if not awaiting_plan_review:
            completed = _advance_run(
                planned,
                runtime_status="Running",
                status="Running",
                phase="Executing",
                message="Planning complete. DTS orchestration is moving into task execution.",
            )
            ctx.set_custom_status(completed.model_dump(mode="json"))
            return completed.model_dump(mode="json")

        waiting = _advance_run(
            planned,
            runtime_status="Running",
            status="WaitingForPlanReview",
            phase="PlanReview",
            message="Plan ready for review. Submit plan feedback when approved.",
        )
        ctx.set_custom_status(waiting.model_dump(mode="json"))
        feedback_payload = yield ctx.wait_for_external_event(PLAN_REVIEW_EVENT_NAME)
        feedback = PlanFeedbackSubmission.model_validate(feedback_payload)
        if feedback.action is PlanFeedbackAction.APPROVED:
            approved = _advance_run(
                _apply_plan_feedback(waiting, feedback),
                runtime_status="Running",
                status="Running",
                phase="Executing",
                message="Plan approved. DTS orchestration is moving into task execution.",
            )
            ctx.set_custom_status(approved.model_dump(mode="json"))
            return approved.model_dump(mode="json")
        run = _advance_run(
            _apply_plan_feedback(waiting, feedback),
            runtime_status="Running",
            status="Running",
            phase="Planning",
            message="Reviewer requested plan changes. DTS is re-running the planner.",
        )
        ctx.set_custom_status(run.model_dump(mode="json"))


planning_sub_orchestration.__name__ = PLANNING_SUB_ORCHESTRATION_NAME


def execution_round_orchestration(ctx, input: dict[str, Any]) -> dict[str, Any]:
    run = _ensure_branch_tracking(_load_runtime_envelope(input).run)
    while True:
        ready_task_indices = _all_ready_task_indices(run)
        if not ready_task_indices:
            break

        scheduled_workers: list[tuple[int, str]] = []
        worker_activities = []
        for task_index in ready_task_indices:
            current_task = run.tasks[task_index]
            started_task = current_task.model_copy(
                update={
                    "status": "Executing",
                    "summary": f"Worker sandbox is executing this task.",
                    "active_sandbox_id": None,
                    "history": [*current_task.history, SwarmTaskStatusTransition(status="Executing")],
                }
            )
            tasks = list(run.tasks)
            tasks[task_index] = started_task
            worker_summary = SwarmActivitySummary(
                id=f"worker-{started_task.id}-{len(run.worker_summaries) + 1}",
                kind="worker",
                title=started_task.title,
                status="running",
                summary=started_task.summary,
                assignee=run.owner.login,
                branch_name=started_task.branch_name,
                round_number=started_task.round_number,
                active_sandbox_id=None,
            )
            run = _advance_run(
                run.model_copy(
                    update={
                        "tasks": tasks,
                        "plan": run.plan.model_copy(update={"tasks": tasks}),
                        "branch_state": run.branch_state.model_copy(update={"merge_status": "Executing"}),
                        "worker_summaries": [*run.worker_summaries, worker_summary],
                        "intent": run.intent.model_copy(update={"resume_requested": False}),
                    }
                ),
                runtime_status="Running",
                status="Running",
                phase="Executing",
                message=f"Worker sandbox is executing '{started_task.title}'.",
            )
            ctx.set_custom_status(run.model_dump(mode="json"))
            worker_activities.append(
                ctx.call_activity(
                    RUN_WORKER_IN_SANDBOX_ACTIVITY_NAME,
                    input=_compact_model_dump(
                        SandboxRoleActivityInput(
                            role="worker",
                            run=run,
                            task=started_task,
                        )
                    ),
                    retry_policy=_SANDBOX_ROLE_RETRY_POLICY,
                )
            )
            scheduled_workers.append((task_index, worker_summary.id))

        try:
            result_payloads = yield when_all(worker_activities)
        except Exception as exc:
            failure_message = str(exc)
            discarded_worker_summary = "Worker wave failed before this result could be applied."
            discarded_worker_details = (
                f"Worker wave failed after another worker exhausted retries: {failure_message}"
            )
            failed_tasks = list(run.tasks)
            failed_worker_summaries = run.worker_summaries
            for (task_index, worker_summary_id), worker_activity in zip(
                scheduled_workers, worker_activities
            ):
                started_task = run.tasks[task_index]
                if worker_activity.is_failed:
                    task_failure_message = _task_exception_message(worker_activity, failure_message)
                    failed_tasks[task_index] = started_task.model_copy(
                        update={
                            "status": "Failed",
                            "summary": "Worker sandbox failed.",
                            "failure_details": task_failure_message,
                            "active_sandbox_id": None,
                            "history": [
                                *started_task.history,
                                SwarmTaskStatusTransition(status="Failed"),
                            ],
                        }
                    )
                    failed_worker_summaries = _replace_last_activity(
                        failed_worker_summaries,
                        activity_id=worker_summary_id,
                        status="failed",
                        summary="Worker sandbox failed.",
                        details=task_failure_message,
                        active_sandbox_id=None,
                    )
                    continue

                failed_tasks[task_index] = started_task.model_copy(
                    update={
                        "status": "Failed",
                        "summary": discarded_worker_summary,
                        "failure_details": discarded_worker_details,
                        "active_sandbox_id": None,
                        "history": [
                            *started_task.history,
                            SwarmTaskStatusTransition(status="Failed"),
                        ],
                    }
                )
                failed_worker_summaries = _replace_last_activity(
                    failed_worker_summaries,
                    activity_id=worker_summary_id,
                    status="failed",
                    summary=discarded_worker_summary,
                    details=discarded_worker_details,
                    active_sandbox_id=None,
                )
            failed = _advance_run(
                run.model_copy(
                    update={
                        "tasks": failed_tasks,
                        "plan": run.plan.model_copy(update={"tasks": failed_tasks}),
                        "failure_message": failure_message,
                        "worker_summaries": failed_worker_summaries,
                    }
                ),
                runtime_status="Failed",
                status="Failed",
                phase="Failed",
                message=(
                    "Worker wave failed after retries were exhausted. "
                    "Results from this wave were not applied."
                ),
            )
            ctx.set_custom_status(failed.model_dump(mode="json"))
            raise

        for (task_index, worker_summary_id), result_payload in zip(scheduled_workers, result_payloads):
            started_task = run.tasks[task_index]
            result = WorkerExecutionResult.model_validate(result_payload)
            completion_message = (
                f"Completed worker validation for '{started_task.title}' without new repo changes and queued it for reviewer validation."
                if result.no_changes
                else f"Completed worker mutation for '{started_task.title}' and queued it for reviewer validation."
            )

            completed_task = started_task.model_copy(
                update={
                    "status": "PendingReview",
                    "summary": result.summary,
                    "branch_name": result.branch_name,
                    "round_number": result.round_number,
                    "failure_details": None,
                    "active_sandbox_id": None,
                    "head_commit_sha": result.head_commit_sha,
                    "parent_commit_sha": result.parent_commit_sha,
                    "changed_files": result.changed_files,
                    "validation_summary": result.validation_summary,
                    "validation_results": result.validation_results,
                    "history": [*started_task.history, SwarmTaskStatusTransition(status="PendingReview")],
                }
            )
            updated_tasks = list(run.tasks)
            updated_tasks[task_index] = completed_task
            head_updated = _apply_worker_completion_result(
                run,
                updated_tasks=updated_tasks,
            )
            run = _advance_run(
                head_updated.model_copy(
                    update={
                        "worker_summaries": _replace_last_activity(
                            run.worker_summaries,
                            activity_id=worker_summary_id,
                            status="completed",
                            summary=result.summary,
                            details=result.details,
                            branch_name=result.branch_name,
                            round_number=result.round_number,
                            active_sandbox_id=None,
                            head_commit_sha=result.head_commit_sha,
                            parent_commit_sha=result.parent_commit_sha,
                            changed_files=result.changed_files,
                            validation_summary=result.validation_summary,
                            validation_results=result.validation_results,
                        ),
                    }
                ),
                runtime_status="Running",
                status="Running",
                phase="Reviewing" if _pending_review_task_indices(head_updated) else "Executing",
                message=completion_message,
            )
            ctx.set_custom_status(run.model_dump(mode="json"))

    review_task_indices = _pending_review_task_indices(run)
    if not review_task_indices:
        if run.tasks and all(task.is_completed for task in run.tasks):
            publishing = _advance_run(
                run.model_copy(update={"consecutive_fix_rounds": 0}),
                runtime_status="Running",
                status="Running",
                phase="Publishing",
                message="All execution tasks are complete. DTS is publishing the sandbox results.",
            )
            ctx.set_custom_status(publishing.model_dump(mode="json"))
            return publishing.model_dump(mode="json")
        ctx.set_custom_status(run.model_dump(mode="json"))
        return run.model_dump(mode="json")

    merge_inputs = _completed_worker_branches_in_task_order(run, task_indices=review_task_indices)
    if merge_inputs:
        merge_scope = "completed worker branches"
        run = _advance_run(
            run.model_copy(
                update={
                    "branch_state": run.branch_state.model_copy(
                        update={
                            "merge_status": "Integrating",
                            "merge_resolution_state": "NotRequired",
                            "merge_resolution_sandbox_id": None,
                        }
                    )
                }
            ),
            runtime_status="Running",
            status="Running",
            phase="Reviewing",
            message=f"Git merge activity is updating the run integration branch from {merge_scope}.",
        )
        ctx.set_custom_status(run.model_dump(mode="json"))
        try:
            merge_result_payload = yield ctx.call_activity(
                GIT_MERGE_ACTIVITY_NAME,
                input=_compact_model_dump(
                    MergeSandboxActivityInput(run=run, worker_branches=merge_inputs)
                ),
                retry_policy=_SANDBOX_ROLE_RETRY_POLICY,
            )
        except Exception as exc:
            failed_tasks = list(run.tasks)
            for index in review_task_indices:
                task = failed_tasks[index]
                failed_tasks[index] = task.model_copy(
                    update={
                        "status": "Failed",
                        "summary": "Git merge activity failed before reviewer validation.",
                        "failure_details": str(exc),
                        "history": [*task.history, SwarmTaskStatusTransition(status="Failed")],
                    }
                )
            failed = _advance_run(
                run.model_copy(
                    update={
                        "tasks": failed_tasks,
                        "plan": run.plan.model_copy(update={"tasks": failed_tasks}),
                        "failure_message": str(exc),
                        "branch_state": run.branch_state.model_copy(
                            update={
                                "merge_status": "Failed",
                                "merge_resolution_state": "NotRequired",
                                "merge_resolution_sandbox_id": None,
                            }
                        ),
                    }
                ),
                runtime_status="Failed",
                status="Failed",
                phase="Failed",
                message="Git merge activity failed.",
            )
            ctx.set_custom_status(failed.model_dump(mode="json"))
            raise
        merge_result = MergeExecutionResult.model_validate(merge_result_payload)
        merge_message = (
            "Integration branch updated from completed worker branches."
            if merge_result.merged_branch_names
            else "Integration branch baseline is ready for reviewer validation."
        )
        run = _advance_run(
            _record_branch_head(
                run,
                branch_name=merge_result.target_branch,
                commit_sha=merge_result.head_commit_sha,
            ),
            runtime_status="Running",
            status="Running",
            phase="Reviewing",
            message=merge_message,
        )
        ctx.set_custom_status(run.model_dump(mode="json"))
    elif not run.branch_state.current_head_sha:
        try:
            head_ready = _establish_review_head_from_no_change_tasks(
                run,
                task_indices=review_task_indices,
            )
        except ValueError as exc:
            failed_tasks = list(run.tasks)
            for index in review_task_indices:
                task = failed_tasks[index]
                failed_tasks[index] = task.model_copy(
                    update={
                        "status": "Failed",
                        "summary": "No-change worker tasks could not establish a reviewable integration head.",
                        "failure_details": str(exc),
                        "history": [*task.history, SwarmTaskStatusTransition(status="Failed")],
                    }
                )
            failed = _advance_run(
                run.model_copy(
                    update={
                        "tasks": failed_tasks,
                        "plan": run.plan.model_copy(update={"tasks": failed_tasks}),
                        "failure_message": str(exc),
                        "branch_state": run.branch_state.model_copy(
                            update={
                                "merge_status": "Failed",
                                "merge_resolution_state": "NotRequired",
                                "merge_resolution_sandbox_id": None,
                            }
                        ),
                    }
                ),
                runtime_status="Failed",
                status="Failed",
                phase="Failed",
                message="No-change worker tasks could not establish a reviewable integration head.",
            )
            ctx.set_custom_status(failed.model_dump(mode="json"))
            raise
        if head_ready.branch_state.current_head_sha:
            run = _advance_run(
                head_ready,
                runtime_status="Running",
                status="Running",
                phase="Reviewing",
                message="No-change worker tasks preserved the integration head without new repo changes for reviewer validation.",
            )
            ctx.set_custom_status(run.model_dump(mode="json"))

    integration_branch = _default_target_branch(run)
    integrated_head_sha = (run.branch_state.current_head_sha or "").strip()
    if not integrated_head_sha:
        failure_message = (
            "Reviewer scheduling requires branch fan-in to establish the integration branch head."
        )
        failed = _advance_run(
            run.model_copy(
                update={
                    "failure_message": failure_message,
                    "branch_state": run.branch_state.model_copy(
                        update={
                            "branch_name": integration_branch,
                            "merge_status": "Failed",
                            "merge_resolution_state": "NotRequired",
                            "merge_resolution_sandbox_id": None,
                        }
                    ),
                }
            ),
            runtime_status="Failed",
            status="Failed",
            phase="Failed",
            message="Branch fan-in did not produce an integration head for reviewer validation.",
        )
        ctx.set_custom_status(failed.model_dump(mode="json"))
        raise RuntimeError(failure_message)
    run = run.model_copy(
        update={
            "target_branch": integration_branch,
            "branch_state": run.branch_state.model_copy(
                update={
                    "branch_name": integration_branch,
                    "current_head_sha": integrated_head_sha,
                }
            ),
        }
    )

    review_tasks = list(run.tasks)
    for index in review_task_indices:
        review_tasks[index] = review_tasks[index].model_copy(
            update={
                "status": "InReview",
                "history": [*review_tasks[index].history, SwarmTaskStatusTransition(status="InReview")],
            }
        )
    review_summary = SwarmActivitySummary(
        id=f"reviewer-{len(run.reviewer_summaries) + 1}",
        kind="reviewer",
        title=f"Review execution round {run.total_execution_rounds + 1}",
        status="running",
        summary="Reviewer sandbox is evaluating the completed execution wave.",
    )
    run = _advance_run(
        run.model_copy(
            update={
                "tasks": review_tasks,
                "plan": run.plan.model_copy(update={"tasks": review_tasks}),
                "active_reviewer_sandbox_id": None,
                "branch_state": run.branch_state.model_copy(
                    update={
                        "merge_status": "PendingReview",
                        "merge_resolution_state": "Active" if run.merge_resolver_sandboxes else "NotRequired",
                        "merge_resolution_sandbox_id": (
                            run.merge_resolver_sandboxes[-1].sandbox_id if run.merge_resolver_sandboxes else None
                        ),
                    }
                ),
                "reviewer_summaries": [*run.reviewer_summaries, review_summary],
            }
        ),
        runtime_status="Running",
        status="Running",
        phase="Reviewing",
        message="Reviewer sandbox is validating the completed run.",
    )
    ctx.set_custom_status(run.model_dump(mode="json"))
    try:
        result_payload = yield ctx.call_activity(
            RUN_REVIEW_IN_SANDBOX_ACTIVITY_NAME,
            input=_compact_model_dump(SandboxRoleActivityInput(role="reviewer", run=run)),
            retry_policy=_SANDBOX_ROLE_RETRY_POLICY,
        )
    except Exception as exc:
        failed = _advance_run(
            run.model_copy(
                update={
                    "active_reviewer_sandbox_id": None,
                    "failure_message": str(exc),
                    "reviewer_summaries": _replace_last_activity(
                        run.reviewer_summaries,
                        status="failed",
                        summary="Reviewer sandbox failed.",
                        details=str(exc),
                        active_sandbox_id=None,
                    ),
                }
            ),
            runtime_status="Failed",
            status="Failed",
            phase="Failed",
            message="Reviewer sandbox failed.",
        )
        ctx.set_custom_status(failed.model_dump(mode="json"))
        raise
    result = ReviewerExecutionResult.model_validate(result_payload)
    reviewed_tasks = list(run.tasks)
    for index in review_task_indices:
        reviewed_tasks[index] = reviewed_tasks[index].model_copy(
            update={
                "status": "Completed",
                "active_sandbox_id": None,
                "history": [*reviewed_tasks[index].history, SwarmTaskStatusTransition(status="Completed")],
            }
        )
    updated = _record_reviewed_head(
        run.model_copy(
            update={
                "tasks": reviewed_tasks,
                "plan": run.plan.model_copy(update={"tasks": reviewed_tasks}),
                "active_reviewer_sandbox_id": None,
                "publish_status": None,
                "publish_error": None,
                "target_branch": result.target_branch or run.target_branch,
                "pull_request_url": result.pull_request_url or run.pull_request_url,
                "pull_request_number": run.pull_request_number,
                "total_execution_rounds": run.total_execution_rounds + 1,
                "reviewer_summaries": _replace_last_activity(
                    run.reviewer_summaries,
                    status="completed",
                    summary=result.summary,
                    details=result.details,
                    findings=result.findings,
                    fix_tasks=result.fix_tasks,
                    replan_summary=result.replan_summary,
                    replan_findings=result.replan_findings,
                    publish_status=None,
                    publish_error=None,
                    pull_request_url=None,
                    pull_request_number=None,
                    active_sandbox_id=None,
                ),
                "intent": run.intent.model_copy(
                    update={
                        "cancel_requested": False,
                        "resume_requested": False,
                        "suspend_requested": False,
                    }
                ),
            }
        ),
        approved=result.outcome is ReviewOutcome.APPROVED,
    )
    if result.outcome is ReviewOutcome.REPLAN:
        next_replan_count = updated.replan_count + 1
        if next_replan_count > updated.options.max_replans:
            capped = _advance_run(
                updated.model_copy(
                    update={
                        "replan_count": next_replan_count,
                        "consecutive_fix_rounds": 0,
                    }
                ),
                runtime_status="Completed",
                status="Completed",
                phase="Completed",
                message="Reviewer requested a replan, but the configured replan limit was reached. Completing best-effort without publish.",
            )
            ctx.set_custom_status(capped.model_dump(mode="json"))
            return capped.model_dump(mode="json")
        replanned_state = _record_reviewed_head(
            updated.model_copy(
                update={
                    "replan_count": next_replan_count,
                    "consecutive_fix_rounds": 0,
                    "pending_replan_summary": result.replan_summary or result.summary,
                    "pending_replan_findings": result.replan_findings,
                    "plan": updated.plan.model_copy(update={"design_document": None, "tasks": []}),
                    "tasks": [],
                    "publish_status": None,
                    "publish_error": None,
                    "branch_state": updated.branch_state.model_copy(update={"merge_status": "ReplanRequested"}),
                }
            ),
            approved=False,
            next_wave=updated.branch_state.active_wave + 1,
            reset_wave_round=True,
        )
        replanned = _advance_run(
            replanned_state,
            runtime_status="Running",
            status="Queued",
            phase="Planning",
            message="Reviewer requested a replan. DTS is returning to the planner with reviewer guidance.",
        )
        ctx.set_custom_status(replanned.model_dump(mode="json"))
        return replanned.model_dump(mode="json")

    if result.outcome is ReviewOutcome.FIX_TASKS:
        next_fix_depth = updated.consecutive_fix_rounds + 1
        if next_fix_depth > updated.options.max_fix_chain_depth:
            capped = _advance_run(
                updated.model_copy(update={"consecutive_fix_rounds": next_fix_depth}),
                runtime_status="Completed",
                status="Completed",
                phase="Completed",
                message="Reviewer found additional fix work, but the configured fix-chain depth was reached. Completing best-effort without publish.",
            )
            ctx.set_custom_status(capped.model_dump(mode="json"))
            return capped.model_dump(mode="json")
        fix_tasks = [
            SwarmTaskState(
                id=item.id,
                title=item.title,
                status="Pending",
                summary=item.description,
                branch_name=item.branch_name,
                round_number=item.round_number,
                dependencies=item.dependencies,
                target_files=item.target_files,
                acceptance_criteria=item.acceptance_criteria,
                history=[SwarmTaskStatusTransition(status="Pending")],
            )
            for item in result.fix_tasks
        ]
        queued = _advance_run(
            updated.model_copy(
                update={
                    "tasks": [*updated.tasks, *fix_tasks],
                    "plan": updated.plan.model_copy(update={"tasks": [*updated.tasks, *fix_tasks]}),
                    "consecutive_fix_rounds": next_fix_depth,
                    "publish_status": None,
                    "publish_error": None,
                    "branch_state": updated.branch_state.model_copy(update={"merge_status": "Executing"}),
                }
            ),
            runtime_status="Running",
            status="Running",
            phase="Executing",
            message="Reviewer queued follow-up fix tasks before publish.",
        )
        ctx.set_custom_status(queued.model_dump(mode="json"))
        return queued.model_dump(mode="json")

    approved = updated.model_copy(update={"consecutive_fix_rounds": 0})
    remaining_ready = _next_ready_task_index(approved)
    if remaining_ready is not None:
        resumed = _advance_run(
            approved,
            runtime_status="Running",
            status="Running",
            phase="Executing",
            message="Reviewer approved the current execution wave. DTS is resuming queued tasks.",
        )
        ctx.set_custom_status(resumed.model_dump(mode="json"))
        return resumed.model_dump(mode="json")
    publishing = _advance_run(
        approved.model_copy(update={"branch_state": approved.branch_state.model_copy(update={"merge_status": "Publishing"})}),
        runtime_status="Running",
        status="Running",
        phase="Publishing",
        message="Reviewer approved the completed run. DTS is publishing the sandbox results.",
    )
    ctx.set_custom_status(publishing.model_dump(mode="json"))
    return publishing.model_dump(mode="json")


execution_round_orchestration.__name__ = EXECUTION_ROUND_ORCHESTRATION_NAME


def swarm_orchestration(ctx, input: dict[str, Any]) -> dict[str, Any]:
    envelope = _load_runtime_envelope(input)
    run = _ensure_branch_tracking(envelope.run)
    history = envelope.history
    while True:
        ctx.set_custom_status(run.model_dump(mode="json"))
        if run.is_terminal:
            return run.model_dump(mode="json")
        if not run.plan.design_document:
            run = SwarmRunState.model_validate(
                _extract_run_payload(
                    (
                        yield ctx.call_sub_orchestrator(
                        PLANNING_SUB_ORCHESTRATION_NAME,
                        input=_runtime_input(_mark_execution_state(run, command_type="plan", worker_id="dts-orchestrator"), history),
                        instance_id=_current_planning_instance_id(run),
                    )
                    )
                )
            )
            continue
        if _next_ready_task_index(run) is not None or _pending_review_task_indices(run):
            previous_round_count = run.total_execution_rounds
            run = SwarmRunState.model_validate(
                _extract_run_payload(
                    (
                        yield ctx.call_sub_orchestrator(
                        EXECUTION_ROUND_ORCHESTRATION_NAME,
                        input=_runtime_input(_mark_execution_state(run, command_type="execute-round", worker_id="dts-orchestrator"), history),
                        instance_id=execution_round_instance_id(run.owner.user_id, run.id, previous_round_count + 1),
                    )
                    )
                )
            )
            if run.total_execution_rounds > previous_round_count:
                history = history.record_round()
            continue
        if run.tasks and all(task.is_completed for task in run.tasks):
            publishing = _advance_run(
                run,
                runtime_status="Running",
                status="Running",
                phase="Publishing",
                message=run.message or "DTS is publishing the sandbox results.",
            )
            ctx.set_custom_status(publishing.model_dump(mode="json"))
            publish_payload = yield ctx.call_activity(
                PUBLISH_TO_GITHUB_ACTIVITY_NAME,
                input=_compact_model_dump(PublishActivityInput(run=publishing)),
            )
            completed = _apply_publish_result(
                publishing,
                GitHubPublishResult.model_validate(publish_payload),
            )
            ctx.set_custom_status(completed.model_dump(mode="json"))
            return completed.model_dump(mode="json")
        completed = _advance_run(
            run,
            runtime_status="Completed",
            status="Completed",
            phase="Completed",
            message="DTS orchestration drained with no remaining work.",
        )
        ctx.set_custom_status(completed.model_dump(mode="json"))
        return completed.model_dump(mode="json")


swarm_orchestration.__name__ = SWARM_ORCHESTRATION_NAME


class DtsSwarmCoordinator(SwarmRunCoordinator):
    def __init__(
        self,
        client: DurableTaskSchedulerClient,
        ownership_store: RunOwnershipStore,
    ) -> None:
        self._client = client
        self._ownership_store = ownership_store

    async def list_runs(self) -> list[SwarmRunState]:
        run_ids = await self._ownership_store.list_all_run_ids()
        runs: list[SwarmRunState] = []
        for run_id in run_ids:
            run = await self.get_run(run_id)
            if run is not None:
                runs.append(run)
        return sorted(runs, key=lambda item: item.created_at_utc, reverse=True)

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
        now = utcnow()
        resolved_run_id = run_id or datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
        repo = parse_repository_context(repository_url, base_branch)
        target_branch = build_integration_branch_name(
            SwarmRunState(
                id=resolved_run_id,
                owner=owner,
                title="",
                prompt=prompt,
                repository_url=repository_url,
                base_branch=base_branch,
                options=options,
            ),
            repo,
        )
        run = SwarmRunState(
            id=resolved_run_id,
            owner=owner,
            title=" ".join(prompt.split())[:80],
            prompt=prompt,
            repository_url=repository_url,
            base_branch=base_branch,
            target_branch=target_branch,
            runtime_status="Pending",
            status="Queued",
            phase="Queued",
            message="Run accepted and scheduled in DTS.",
            created_at_utc=now,
            last_updated_at_utc=now,
            options=options,
            branch_state={
                "branch_name": target_branch,
                "active_wave": 1,
                "current_wave_round": 0,
                "merge_status": "Queued",
                "merge_resolution_state": "NotRequired",
            },
            checkpoint=CoordinatorCheckpoint(run_id="", phase="Queued", status="Accepted", sequence=0),
        )
        run = run.model_copy(
            update={
                "title": _derive_title(prompt),
                "checkpoint": run.checkpoint.model_copy(update={"run_id": run.id}),
            }
        )
        await self._ownership_store.add(run)
        try:
            await asyncio.to_thread(
                self._client.schedule_new_orchestration,
                swarm_orchestration,
                input=_runtime_input(run),
                instance_id=run.id,
                tags={"ownerSessionId": owner.session_id, "ownerLabel": owner.login},
            )
        except Exception:
            await self._ownership_store.remove(owner.user_id, run.id)
            raise
        return run

    async def get_run(self, run_id: str) -> SwarmRunState | None:
        state = await asyncio.to_thread(self._client.get_orchestration_state, run_id, fetch_payloads=True)
        if state is None:
            return None
        run = _hydrate_run_state(state)
        if run is None:
            return None
        planning_state = await asyncio.to_thread(
            self._client.get_orchestration_state,
            _current_planning_instance_id(run),
            fetch_payloads=True,
        )
        run, state = _prefer_sub_orchestration_run(run, state, planning_state)
        execution_state = await asyncio.to_thread(
            self._client.get_orchestration_state,
            execution_round_instance_id(run.owner.user_id, run.id, run.total_execution_rounds + 1),
            fetch_payloads=True,
        )
        run, _ = _prefer_sub_orchestration_run(run, state, execution_state)
        return run

    async def get_projection(self, run_id: str):
        run = await self.get_run(run_id)
        return None if run is None else build_projection_snapshot(run)

    async def submit_plan_feedback(
        self,
        run_id: str,
        feedback: PlanFeedbackSubmission,
    ) -> SwarmRunState | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        await asyncio.to_thread(
            self._client.raise_orchestration_event,
            _current_planning_instance_id(run),
            PLAN_REVIEW_EVENT_NAME,
            data=feedback.model_dump(mode="json"),
        )
        return run

    async def request_cancel(self, run_id: str) -> SwarmRunState | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        await asyncio.to_thread(self._client.terminate_orchestration, run_id, output={"reason": "cancelled-by-user"})
        return run.model_copy(
            update={
                "runtime_status": "Terminated",
                "status": "Cancelled",
                "phase": "Cancelled",
                "message": "Run cancelled by request.",
                "last_updated_at_utc": utcnow(),
            }
        )

    async def request_suspend(
        self,
        run_id: str,
        reason: str | None = None,
    ) -> SwarmRunState | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        await asyncio.to_thread(self._client.suspend_orchestration, run_id)
        return run.model_copy(
            update={
                "runtime_status": "Suspended",
                "status": "Suspended",
                "phase": "Suspended",
                "message": reason or "Run suspended through DTS.",
                "last_updated_at_utc": utcnow(),
            }
        )

    async def request_resume(
        self,
        run_id: str,
        reason: str | None = None,
    ) -> SwarmRunState | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        await asyncio.to_thread(self._client.resume_orchestration, run_id)
        return run.model_copy(
            update={
                "runtime_status": "Running" if run.plan.design_document else "Pending",
                "status": "Queued" if not run.plan.design_document else "Running",
                "phase": "Queued" if not run.plan.design_document else run.phase,
                "message": reason or "Run resumed through DTS.",
                "last_updated_at_utc": utcnow(),
            }
        )

    async def purge_run(self, run_id: str) -> bool:
        run = await self.get_run(run_id)
        if run is None:
            return False
        await asyncio.to_thread(self._client.purge_orchestration, run_id)
        await self._ownership_store.remove(run.owner.user_id, run_id)
        return True

    async def rerun(self, run_id: str, owner: RunOwner, *, new_run_id: str | None = None) -> SwarmRunState | None:
        existing = await self.get_run(run_id)
        if existing is None:
            return None
        return await self.create_run(
            owner=owner,
            prompt=existing.prompt,
            repository_url=existing.repository_url,
            base_branch=existing.base_branch,
            options=existing.options,
            run_id=new_run_id,
        )


class DtsSwarmRuntimeHost:
    def __init__(
        self,
        settings: ServiceSettings,
        *,
        sandbox_lifecycle: AcaSandboxLifecycleExecutor,
        sandbox_client: Any,
        publish_service: GitHubPublisherProtocol,
    ) -> None:
        connection_string = settings.dts.connection_string.get_secret_value() if settings.dts.connection_string else None
        if not connection_string:
            raise ValueError("DTS orchestration backend requires DTS_CONNECTION_STRING.")
        info = DtsConnectionInfo.from_connection_string(connection_string)
        credential = DefaultAzureCredential(managed_identity_client_id=info.client_id)
        self._client = DurableTaskSchedulerClient(
            host_address=info.host_address,
            secure_channel=info.secure_channel,
            taskhub=info.taskhub,
            token_credential=credential,
        )
        self._worker = DurableTaskSchedulerWorker(
            host_address=info.host_address,
            secure_channel=info.secure_channel,
            taskhub=info.taskhub,
            token_credential=credential,
        )
        self._worker.add_orchestrator(swarm_orchestration)
        self._worker.add_orchestrator(planning_sub_orchestration)
        self._worker.add_orchestrator(execution_round_orchestration)
        self._worker.add_activity(_make_run_planner_activity(sandbox_lifecycle, sandbox_client))
        self._worker.add_activity(_make_run_worker_activity(sandbox_lifecycle, sandbox_client))
        self._worker.add_activity(_make_run_review_activity(sandbox_lifecycle, sandbox_client))
        self._worker.add_activity(_make_git_merge_activity(sandbox_lifecycle, sandbox_client))
        self._worker.add_activity(_make_publish_activity(publish_service))

    @property
    def client(self) -> DurableTaskSchedulerClient:
        return self._client

    async def start(self) -> None:
        await asyncio.to_thread(self._worker.start)

    async def stop(self) -> None:
        await asyncio.to_thread(self._worker.stop)


def _hydrate_run_state(state: OrchestrationState) -> SwarmRunState | None:
    payload = state.serialized_custom_status or state.serialized_output or state.serialized_input
    if not payload:
        return None
    payload_data = json.loads(payload)
    run = SwarmRunState.model_validate(_extract_run_payload(payload_data))
    hydrated_last_updated_at = _coerce_utc_timestamp(run.last_updated_at_utc)
    state_last_updated_at = _coerce_utc_timestamp(state.last_updated_at)
    failure_message = run.failure_message
    if state.failure_details is not None and getattr(state.failure_details, "message", None):
        failure_message = state.failure_details.message
    updates: dict[str, Any] = {
        "last_updated_at_utc": max(hydrated_last_updated_at, state_last_updated_at),
        "failure_message": failure_message,
    }
    if state.runtime_status is OrchestrationStatus.PENDING:
        updates["runtime_status"] = "Pending"
    elif state.runtime_status is OrchestrationStatus.RUNNING:
        updates["runtime_status"] = run.runtime_status if run.runtime_status != "Pending" else "Running"
    elif state.runtime_status is OrchestrationStatus.SUSPENDED:
        updates.update({"runtime_status": "Suspended", "status": "Suspended", "phase": "Suspended"})
    elif state.runtime_status is OrchestrationStatus.TERMINATED:
        updates.update({"runtime_status": "Terminated", "status": "Cancelled", "phase": "Cancelled"})
    elif state.runtime_status is OrchestrationStatus.FAILED:
        updates.update({"runtime_status": "Failed", "status": "Failed", "phase": "Failed"})
    elif state.runtime_status is OrchestrationStatus.COMPLETED and not run.is_terminal:
        updates.update({"runtime_status": "Completed", "status": "Completed", "phase": "Completed"})
    return run.model_copy(update=updates)
