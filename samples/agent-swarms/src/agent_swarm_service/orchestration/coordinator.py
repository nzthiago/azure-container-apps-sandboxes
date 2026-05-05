from __future__ import annotations

import asyncio
import contextlib
import uuid
from collections.abc import Sequence
from typing import Any, Protocol

from agent_swarm_service.github.publishing import GitHubPublisherProtocol, GitHubPublishResult
from agent_swarm_service.orchestration.models import (
    CoordinatorCheckpoint,
    CoordinatorCommand,
    PlanFeedbackAction,
    PlanFeedbackSubmission,
    ReviewOutcome,
    RunLease,
    RunProjectionSnapshot,
    RunOwner,
    SwarmActivitySummary,
    SwarmOptions,
    SwarmRunState,
    SwarmTaskState,
    SwarmTaskStatusTransition,
    utcnow,
)
from agent_swarm_service.orchestration.projections import build_projection_snapshot
from agent_swarm_service.orchestration.sandbox_execution import (
    AcaSandboxLifecycleExecutor,
    build_integration_branch_name,
    parse_repository_context,
    planned_task,
)
from agent_swarm_service.runtime.storage import QueueMessage, RuntimeStorageBackendProtocol
from agent_swarm_service.sandboxes.aca_client import SandboxHandle, attach_sandbox_context, sandbox_id

_UNSET = object()


class SwarmRunCoordinator(Protocol):
    async def list_runs(self) -> Sequence[SwarmRunState]: ...

    async def create_run(
        self,
        *,
        owner: RunOwner,
        prompt: str,
        repository_url: str,
        base_branch: str | None,
        options: SwarmOptions,
        run_id: str | None = None,
    ) -> SwarmRunState: ...

    async def get_run(self, run_id: str) -> SwarmRunState | None: ...

    async def get_projection(self, run_id: str) -> RunProjectionSnapshot | None: ...

    async def submit_plan_feedback(
        self,
        run_id: str,
        feedback: PlanFeedbackSubmission,
    ) -> SwarmRunState | None: ...

    async def request_cancel(self, run_id: str) -> SwarmRunState | None: ...

    async def request_suspend(
        self,
        run_id: str,
        reason: str | None = None,
    ) -> SwarmRunState | None: ...

    async def request_resume(
        self,
        run_id: str,
        reason: str | None = None,
    ) -> SwarmRunState | None: ...

    async def purge_run(self, run_id: str) -> bool: ...

    async def rerun(self, run_id: str, owner: RunOwner, *, new_run_id: str | None = None) -> SwarmRunState | None: ...


class DurableSwarmCoordinator:
    def __init__(
        self,
        storage: RuntimeStorageBackendProtocol,
        *,
        queue_name: str,
        lease_duration_seconds: int,
    ) -> None:
        self._storage = storage
        self._queue_name = queue_name
        self._lease_duration_seconds = lease_duration_seconds

    async def list_runs(self) -> Sequence[SwarmRunState]:
        documents = await self._storage.list_json("runs/")
        runs = [
            SwarmRunState.model_validate(payload)
            for path, payload in documents
            if path.endswith("/state.json")
        ]
        return sorted(runs, key=lambda run: run.created_at_utc, reverse=True)

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
        run = SwarmRunState(
            id=run_id or uuid.uuid4().hex,
            owner=owner,
            title=_derive_title(prompt),
            prompt=prompt,
            repository_url=repository_url,
            base_branch=base_branch,
            message="Run accepted and queued for the durable coordinator.",
            created_at_utc=now,
            last_updated_at_utc=now,
            options=options,
            tasks=[],
            checkpoint=CoordinatorCheckpoint(run_id="", phase="Queued", status="Accepted", sequence=0),
        )
        run.checkpoint = run.checkpoint.model_copy(update={"run_id": run.id})
        await self._persist_run(run, command_type="run-created")
        return run

    async def get_run(self, run_id: str) -> SwarmRunState | None:
        documents = await self._storage.list_json("runs/")
        for path, payload in documents:
            if path.endswith(f"/{run_id}/state.json"):
                return SwarmRunState.model_validate(payload)
        return None

    async def get_projection(self, run_id: str) -> RunProjectionSnapshot | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        payload = await self._storage.read_json(self._projection_path(run.owner.user_id, run_id))
        if payload is not None:
            return RunProjectionSnapshot.model_validate(payload)
        projection = build_projection_snapshot(run)
        await self._storage.write_json(
            self._projection_path(run.owner.user_id, run_id),
            projection.model_dump(mode="json"),
        )
        return projection

    async def submit_plan_feedback(
        self,
        run_id: str,
        feedback: PlanFeedbackSubmission,
    ) -> SwarmRunState | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        revised_tasks = [
            planned_task(item.id, item.title, item.description)
            for item in feedback.revised_tasks
        ]
        updates = {
            "awaiting_plan_review": False,
            "plan_feedback_history": [*run.plan_feedback_history, feedback],
            "runtime_status": "Pending",
            "status": "Queued",
            "phase": "Queued",
            "message": "Plan feedback captured for coordinator pickup.",
            "last_updated_at_utc": utcnow(),
            "checkpoint": self._next_checkpoint(run, phase="Queued", status="Queued"),
            "intent": run.intent.model_copy(update={"resume_requested": False}),
        }
        if revised_tasks:
            updates["tasks"] = revised_tasks
            updates["plan"] = run.plan.model_copy(update={"tasks": revised_tasks, "design_document": None})
        elif feedback.action is PlanFeedbackAction.REQUEST_CHANGES:
            updates["plan"] = run.plan.model_copy(update={"design_document": None})
        updated = run.model_copy(update=updates)
        await self._persist_run(updated, command_type="plan-feedback")
        return updated

    async def request_cancel(self, run_id: str) -> SwarmRunState | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        if run.is_terminal:
            return run
        runtime_status = "Terminated" if run.runtime_status == "Pending" else run.runtime_status
        status = "Cancelled" if run.runtime_status == "Pending" else "Cancelling"
        phase = "Cancelled" if run.runtime_status == "Pending" else run.phase
        message = (
            "Run cancelled before execution started."
            if run.runtime_status == "Pending"
            else "Cancellation requested; the coordinator will stop after the current durable step."
        )
        updated = run.model_copy(
            update={
                "runtime_status": runtime_status,
                "status": status,
                "phase": phase,
                "message": message,
                "intent": run.intent.model_copy(update={"cancel_requested": True}),
                "last_updated_at_utc": utcnow(),
                "checkpoint": self._next_checkpoint(run, phase=phase, status=status),
            }
        )
        await self._persist_run(updated, command_type="cancel-requested")
        return updated

    async def request_suspend(
        self,
        run_id: str,
        reason: str | None = None,
    ) -> SwarmRunState | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        if run.is_terminal:
            return run
        if run.runtime_status == "Pending":
            runtime_status = "Suspended"
            status = "Suspended"
            phase = "Suspended"
            message = reason or "Run suspended before background execution started."
        else:
            runtime_status = run.runtime_status
            status = "Suspending"
            phase = run.phase
            message = reason or "Suspend requested; the coordinator will pause after the current durable step."
        updated = run.model_copy(
            update={
                "runtime_status": runtime_status,
                "status": status,
                "phase": phase,
                "message": message,
                "intent": run.intent.model_copy(update={"suspend_requested": True, "resume_requested": False}),
                "last_updated_at_utc": utcnow(),
                "checkpoint": self._next_checkpoint(run, phase=phase, status=status),
            }
        )
        await self._persist_run(updated, command_type="suspend-requested")
        return updated

    async def request_resume(
        self,
        run_id: str,
        reason: str | None = None,
    ) -> SwarmRunState | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        if run.is_terminal:
            return run
        updated = run.model_copy(
            update={
                "runtime_status": "Pending" if run.runtime_status == "Suspended" else run.runtime_status,
                "status": "Queued" if run.runtime_status == "Suspended" else run.status,
                "phase": "Queued" if run.runtime_status == "Suspended" else run.phase,
                "message": reason or "Run resumed and re-queued for coordinator pickup.",
                "intent": run.intent.model_copy(update={"suspend_requested": False, "resume_requested": True}),
                "last_updated_at_utc": utcnow(),
                "checkpoint": self._next_checkpoint(
                    run,
                    phase="Queued" if run.runtime_status == "Suspended" else run.phase,
                    status="Queued" if run.runtime_status == "Suspended" else run.status,
                ),
            }
        )
        await self._persist_run(updated, command_type="resume-requested")
        return updated

    async def purge_run(self, run_id: str) -> bool:
        run = await self.get_run(run_id)
        if run is None:
            return False
        await self._storage.delete(self._state_path(run.owner.user_id, run_id))
        await self._storage.delete(self._projection_path(run.owner.user_id, run_id))
        await self._storage.delete(self._lease_path(run.owner.user_id, run_id))
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

    async def try_acquire_lease(self, run_id: str, owner_session_id: str, holder_id: str) -> RunLease | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        lease = await self._storage.acquire_lease(self._lease_path(owner_session_id, run_id), holder_id, self._lease_duration_seconds)
        if lease is None:
            return None
        updated = run.model_copy(update={"lease": lease})
        await self._storage.write_json(self._state_path(owner_session_id, run_id), updated.model_dump(mode="json"))
        return lease

    async def renew_lease(self, run_id: str, owner_session_id: str, lease: RunLease) -> RunLease | None:
        run = await self.get_run(run_id)
        if run is None:
            return None
        renewed = await self._storage.renew_lease(
            self._lease_path(owner_session_id, run_id),
            lease,
            self._lease_duration_seconds,
        )
        if renewed is None:
            return None
        updated = run.model_copy(update={"lease": renewed})
        await self._storage.write_json(self._state_path(owner_session_id, run_id), updated.model_dump(mode="json"))
        return renewed

    async def release_lease(self, run_id: str, owner_session_id: str, lease: RunLease | None) -> bool:
        if lease is None:
            return False
        run = await self.get_run(run_id)
        if run is None:
            return False
        await self._storage.release_lease(self._lease_path(owner_session_id, run_id), lease)
        updated = run.model_copy(update={"lease": None})
        await self._storage.write_json(self._state_path(owner_session_id, run_id), updated.model_dump(mode="json"))
        return True

    async def persist_progress(
        self,
        run: SwarmRunState,
        *,
        status: str,
        phase: str,
        runtime_status: str | None = None,
        message: str | None = None,
        command_type: str | None = None,
        queue_follow_up: bool = False,
    ) -> SwarmRunState:
        updated = run.model_copy(
            update={
                "runtime_status": runtime_status or run.runtime_status,
                "status": status,
                "phase": phase,
                "message": message if message is not None else run.message,
                "last_updated_at_utc": utcnow(),
                "checkpoint": self._next_checkpoint(run, phase=phase, status=status),
            }
        )
        await self._persist_run(updated, command_type=command_type)
        if queue_follow_up:
            await self.enqueue_command(updated, command_type="continue-run", reason=updated.message)
        return updated

    async def persist_state(self, run: SwarmRunState) -> SwarmRunState:
        await self._persist_run(run, command_type=None)
        return run

    async def enqueue_command(self, run: SwarmRunState, *, command_type: str, reason: str | None = None) -> None:
        command = CoordinatorCommand(
            run_id=run.id,
            command_type=command_type,
            requested_by=run.owner,
            reason=reason,
        )
        await self._storage.enqueue(self._queue_name, command.model_dump(mode="json"))

    def _next_checkpoint(self, run: SwarmRunState, *, phase: str, status: str) -> CoordinatorCheckpoint:
        return CoordinatorCheckpoint(
            run_id=run.id,
            phase=phase,
            status=status,
            sequence=(run.checkpoint.sequence + 1) if run.checkpoint else 1,
        )

    async def _persist_run(self, run: SwarmRunState, *, command_type: str | None) -> None:
        await self._storage.write_json(self._state_path(run.owner.user_id, run.id), run.model_dump(mode="json"))
        projection = build_projection_snapshot(run)
        await self._storage.write_json(
            self._projection_path(run.owner.user_id, run.id),
            projection.model_dump(mode="json"),
        )
        if command_type is not None:
            await self.enqueue_command(run, command_type=command_type, reason=run.message)

    @staticmethod
    def _runs_prefix(owner_session_id: str) -> str:
        return f"runs/{owner_session_id}/"

    @classmethod
    def _state_path(cls, owner_session_id: str, run_id: str) -> str:
        return f"{cls._run_root(owner_session_id, run_id)}/state.json"

    @classmethod
    def _projection_path(cls, owner_session_id: str, run_id: str) -> str:
        return f"{cls._run_root(owner_session_id, run_id)}/projection.json"

    @classmethod
    def _lease_path(cls, owner_session_id: str, run_id: str) -> str:
        return f"{cls._run_root(owner_session_id, run_id)}/lease.json"

    @staticmethod
    def _run_root(owner_session_id: str, run_id: str) -> str:
        return f"runs/{owner_session_id}/{run_id}"


class DurableCoordinatorExecutionLoop:
    def __init__(
        self,
        coordinator: DurableSwarmCoordinator,
        *,
        sandbox_lifecycle: AcaSandboxLifecycleExecutor,
        sandbox_client: Any,
        publish_service: GitHubPublisherProtocol,
        poll_interval_seconds: float = 1.0,
        visibility_timeout_seconds: int = 30,
        worker_id: str | None = None,
    ) -> None:
        self._coordinator = coordinator
        self._storage = coordinator._storage
        self._queue_name = coordinator._queue_name
        self._sandbox_lifecycle = sandbox_lifecycle
        self._sandbox_client = sandbox_client
        self._publish_service = publish_service
        self._poll_interval_seconds = poll_interval_seconds
        self._visibility_timeout_seconds = visibility_timeout_seconds
        self._worker_id = worker_id or f"coordinator-{uuid.uuid4().hex[:12]}"
        self._task: asyncio.Task[None] | None = None
        self._stopped = asyncio.Event()
        self._stopped.set()

    @property
    def worker_id(self) -> str:
        return self._worker_id

    async def start(self) -> None:
        if self._task is not None and not self._task.done():
            return
        self._stopped.clear()
        self._task = asyncio.create_task(self._run_forever(), name=f"{self._worker_id}-background-loop")

    async def stop(self) -> None:
        task = self._task
        if task is None:
            return
        task.cancel()
        with contextlib.suppress(asyncio.CancelledError):
            await task
        self._task = None
        self._stopped.set()

    async def wait_stopped(self) -> None:
        await self._stopped.wait()

    async def process_next_message(self) -> bool:
        message = await self._storage.dequeue(
            self._queue_name,
            visibility_timeout_seconds=self._visibility_timeout_seconds,
        )
        if message is None:
            return False
        should_complete = await self._process_message(message)
        if should_complete:
            await self._storage.complete(self._queue_name, message)
        return True

    async def drain(self, *, max_messages: int = 100) -> int:
        processed = 0
        while processed < max_messages and await self.process_next_message():
            processed += 1
        return processed

    async def _run_forever(self) -> None:
        try:
            while True:
                processed = await self.process_next_message()
                if not processed:
                    await asyncio.sleep(self._poll_interval_seconds)
        finally:
            self._stopped.set()

    async def _process_message(self, message: QueueMessage) -> bool:
        command = CoordinatorCommand.model_validate(message.payload)
        run = await self._coordinator.get_run(command.run_id)
        if run is None:
            return True

        lease = await self._coordinator.try_acquire_lease(run.id, run.owner.user_id, self._worker_id)
        if lease is None:
            return False

        try:
            run = await self._coordinator.get_run(run.id)
            if run is None:
                return True
            run = await self._mark_execution_state(
                run,
                lease=lease,
                command_type=command.command_type,
                increment_attempt=True,
            )
            renewed = await self._coordinator.renew_lease(run.id, run.owner.user_id, lease)
            if renewed is not None:
                lease = renewed
                run = await self._mark_execution_state(run, lease=lease, command_type=command.command_type)
            updated = await self._advance_run(run, command.command_type)
            if updated is not None and _should_continue(updated):
                await self._coordinator.enqueue_command(updated, command_type="continue-run", reason=updated.message)
            return True
        finally:
            await self._coordinator.release_lease(run.id, run.owner.user_id, lease)

    async def _mark_execution_state(
        self,
        run: SwarmRunState,
        *,
        lease: RunLease,
        command_type: str,
        increment_attempt: bool = False,
    ) -> SwarmRunState:
        execution = run.execution.model_copy(
            update={
                "owner_id": self._worker_id,
                "last_command_type": command_type,
                "acquired_at_utc": lease.acquired_at_utc,
                "heartbeat_at_utc": lease.heartbeat_at_utc,
                "last_progress_at_utc": utcnow(),
                "attempt_count": run.execution.attempt_count + (1 if increment_attempt else 0),
            }
        )
        updated = run.model_copy(update={"lease": lease, "execution": execution})
        await self._coordinator.persist_state(updated)
        return updated

    async def _advance_run(self, run: SwarmRunState, command_type: str) -> SwarmRunState | None:
        if run.is_terminal:
            return await self._coordinator.persist_state(
                run.model_copy(update={"message": run.message or "Terminal run ignored by coordinator."})
            )

        if run.intent.cancel_requested:
            return await self._terminate_run(run)

        if run.runtime_status == "Suspended" and not run.intent.resume_requested:
            return await self._coordinator.persist_state(run)

        if run.intent.suspend_requested and run.runtime_status != "Suspended":
            return await self._suspend_run(run)

        if not run.plan.design_document:
            return await self._plan_run(run)

        if run.awaiting_plan_review:
            return await self._coordinator.persist_state(run)

        next_task_index = _next_ready_task_index(run)
        if next_task_index is not None:
            return await self._execute_task(run, next_task_index)

        if _pending_review_task_indices(run):
            return await self._review_run(run)

        if run.tasks and all(task.is_completed for task in run.tasks):
            return await self._coordinator.persist_progress(
                run,
                runtime_status="Completed",
                status="Completed",
                phase="Completed",
                message="Coordinator completed the current task graph.",
            )

        return await self._coordinator.persist_progress(
            run,
            runtime_status="Completed",
            status="Completed",
            phase="Completed",
            message=f"Coordinator drained '{command_type}' with no remaining work.",
        )

    async def _plan_run(self, run: SwarmRunState) -> SwarmRunState:
        try:
            sandbox = await self._create_role_sandbox("planner", run)
        except Exception as exc:
            failed = run.model_copy(update={"failure_message": str(exc)})
            return await self._coordinator.persist_progress(
                failed,
                runtime_status="Failed",
                status="Failed",
                phase="Failed",
                message="Planner sandbox could not be created.",
            )
        sandbox_id_value = sandbox_id(sandbox)
        planner_summary = SwarmActivitySummary(
            id=f"planner-{run.checkpoint.sequence + 1 if run.checkpoint else 1}",
            kind="planner",
            title="Create execution plan",
            status="running",
            summary="Planner sandbox is preparing the execution plan.",
            active_sandbox_id=sandbox_id_value,
        )
        in_progress = run.model_copy(
            update={
                "active_planner_sandbox_id": sandbox_id_value,
                "planner_summaries": [*run.planner_summaries, planner_summary],
                "intent": run.intent.model_copy(update={"resume_requested": False}),
            }
        )
        in_progress = await self._coordinator.persist_progress(
            in_progress,
            runtime_status="Running",
            status="Running",
            phase="Planning",
            message=f"Planner sandbox '{sandbox_id_value}' is generating the plan.",
        )
        try:
            result = await self._sandbox_lifecycle.execute_planner(in_progress, sandbox)
        except Exception as exc:
            await self._cleanup_sandbox(sandbox, failed=True)
            failed = in_progress.model_copy(
                update={
                    "active_planner_sandbox_id": None,
                    "failure_message": str(exc),
                    "planner_summaries": _replace_last_activity(
                        in_progress.planner_summaries,
                        status="failed",
                        summary="Planner sandbox failed.",
                        details=str(exc),
                        active_sandbox_id=None,
                    ),
                }
            )
            return await self._coordinator.persist_progress(
                failed,
                runtime_status="Failed",
                status="Failed",
                phase="Failed",
                message="Planner sandbox failed.",
            )

        await self._cleanup_sandbox(sandbox)
        plan = result.to_plan()
        awaiting_plan_review = run.options.planning.human_review_mode.value == "Required"
        updated = in_progress.model_copy(
            update={
                "plan": plan,
                "tasks": plan.tasks,
                "active_planner_sandbox_id": None,
                "awaiting_plan_review": awaiting_plan_review,
                "pending_replan_summary": None,
                "pending_replan_findings": [],
                "planner_summaries": _replace_last_activity(
                    in_progress.planner_summaries,
                    status="completed",
                    summary=result.summary,
                    details=result.design_document,
                    active_sandbox_id=None,
                ),
            }
        )
        if awaiting_plan_review:
            return await self._coordinator.persist_progress(
                updated,
                runtime_status="Running",
                status="WaitingForPlanReview",
                phase="PlanReview",
                message="Plan ready for review. Submit plan feedback or resume execution when approved.",
            )
        return await self._coordinator.persist_progress(
            updated,
            runtime_status="Running",
            status="Running",
            phase="Executing",
            message="Planning complete. Background coordinator is moving into task execution.",
        )

    async def _execute_task(self, run: SwarmRunState, task_index: int) -> SwarmRunState:
        current_task = run.tasks[task_index]
        try:
            sandbox = await self._create_role_sandbox("worker", run, current_task)
        except Exception as exc:
            failed_task = current_task.model_copy(
                update={
                    "status": "Failed",
                    "summary": "Worker sandbox could not be created.",
                    "failure_details": str(exc),
                    "history": [*current_task.history, SwarmTaskStatusTransition(status="Failed")],
                }
            )
            tasks = list(run.tasks)
            tasks[task_index] = failed_task
            failed = run.model_copy(
                update={
                    "tasks": tasks,
                    "plan": run.plan.model_copy(update={"tasks": tasks}),
                    "failure_message": str(exc),
                }
            )
            return await self._coordinator.persist_progress(
                failed,
                runtime_status="Failed",
                status="Failed",
                phase="Failed",
                message=f"Worker sandbox could not be created for '{current_task.title}'.",
            )
        sandbox_id_value = sandbox_id(sandbox)
        started = current_task.model_copy(
            update={
                "status": "Executing",
                "summary": f"Worker sandbox '{sandbox_id_value}' is executing this task.",
                "active_sandbox_id": sandbox_id_value,
                "history": [*current_task.history, SwarmTaskStatusTransition(status="Executing")],
            }
        )
        tasks = list(run.tasks)
        tasks[task_index] = started
        worker_summary = SwarmActivitySummary(
            id=f"worker-{started.id}-{len(run.worker_summaries) + 1}",
            kind="worker",
            title=started.title,
            status="running",
            summary=started.summary,
            assignee=run.owner.login,
            round_number=started.round_number,
            active_sandbox_id=sandbox_id_value,
        )
        in_progress = run.model_copy(
            update={
                "tasks": tasks,
                "plan": run.plan.model_copy(update={"tasks": tasks}),
                "worker_summaries": [*run.worker_summaries, worker_summary],
                "intent": run.intent.model_copy(update={"resume_requested": False}),
            }
        )
        in_progress = await self._coordinator.persist_progress(
            in_progress,
            runtime_status="Running",
            status="Running",
            phase="Executing",
            message=f"Worker sandbox '{sandbox_id_value}' is executing '{started.title}'.",
        )
        try:
            result = await self._sandbox_lifecycle.execute_worker(in_progress, started, sandbox)
        except Exception as exc:
            await self._cleanup_sandbox(sandbox, failed=True)
            failed_task = started.model_copy(
                update={
                    "status": "Failed",
                    "summary": "Worker sandbox failed.",
                    "failure_details": str(exc),
                    "active_sandbox_id": None,
                    "history": [*started.history, SwarmTaskStatusTransition(status="Failed")],
                }
            )
            failed_tasks = list(in_progress.tasks)
            failed_tasks[task_index] = failed_task
            failed = in_progress.model_copy(
                update={
                    "tasks": failed_tasks,
                    "plan": in_progress.plan.model_copy(update={"tasks": failed_tasks}),
                    "failure_message": str(exc),
                    "worker_summaries": _replace_last_activity(
                        in_progress.worker_summaries,
                        status="failed",
                        summary="Worker sandbox failed.",
                        details=str(exc),
                        active_sandbox_id=None,
                    ),
                }
            )
            return await self._coordinator.persist_progress(
                failed,
                runtime_status="Failed",
                status="Failed",
                phase="Failed",
                message=f"Task '{started.title}' failed in its worker sandbox.",
            )

        await self._cleanup_sandbox(sandbox)
        completed = started.model_copy(
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
                "history": [*started.history, SwarmTaskStatusTransition(status="PendingReview")],
            }
        )
        tasks = list(in_progress.tasks)
        tasks[task_index] = completed
        updated = in_progress.model_copy(
            update={
                "tasks": tasks,
                "plan": in_progress.plan.model_copy(update={"tasks": tasks}),
                "worker_summaries": _replace_last_activity(
                    in_progress.worker_summaries,
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
        )
        has_remaining = _next_ready_task_index(updated) is not None
        has_pending_review = bool(_pending_review_task_indices(updated))
        completion_message = (
            f"Completed worker validation for '{completed.title}' without new repo changes and queued it for reviewer validation."
            if result.no_changes
            else f"Completed worker mutation for '{completed.title}' and queued it for reviewer validation."
        )
        return await self._coordinator.persist_progress(
            updated,
            runtime_status="Running",
            status="Running",
            phase="Executing" if has_remaining else "Reviewing" if has_pending_review else "Executing",
            message=completion_message,
        )

    async def _review_run(self, run: SwarmRunState) -> SwarmRunState:
        review_task_indices = _pending_review_task_indices(run)
        if not review_task_indices:
            return await self._coordinator.persist_state(run)
        try:
            sandbox = await self._create_role_sandbox("reviewer", run)
        except Exception as exc:
            failed = run.model_copy(update={"failure_message": str(exc)})
            return await self._coordinator.persist_progress(
                failed,
                runtime_status="Failed",
                status="Failed",
                phase="Failed",
                message="Reviewer sandbox could not be created.",
            )
        sandbox_id_value = sandbox_id(sandbox)
        review_summary = SwarmActivitySummary(
            id=f"reviewer-{len(run.reviewer_summaries) + 1}",
            kind="reviewer",
            title=f"Review execution round {run.total_execution_rounds + 1}",
            status="running",
            summary="Reviewer sandbox is evaluating the completed execution wave.",
            active_sandbox_id=sandbox_id_value,
        )
        review_tasks = list(run.tasks)
        for index in review_task_indices:
            review_tasks[index] = review_tasks[index].model_copy(
                update={
                    "status": "InReview",
                    "history": [*review_tasks[index].history, SwarmTaskStatusTransition(status="InReview")],
                }
            )
        in_progress = run.model_copy(
            update={
                "tasks": review_tasks,
                "plan": run.plan.model_copy(update={"tasks": review_tasks}),
                "active_reviewer_sandbox_id": sandbox_id_value,
                "reviewer_summaries": [*run.reviewer_summaries, review_summary],
            }
        )
        in_progress = await self._coordinator.persist_progress(
            in_progress,
            runtime_status="Running",
            status="Running",
            phase="Reviewing",
            message=f"Reviewer sandbox '{sandbox_id_value}' is validating the completed run.",
        )
        try:
            result = await self._sandbox_lifecycle.execute_reviewer(in_progress, sandbox)
        except Exception as exc:
            await self._cleanup_sandbox(sandbox, failed=True)
            failed = in_progress.model_copy(
                update={
                    "active_reviewer_sandbox_id": None,
                    "failure_message": str(exc),
                    "reviewer_summaries": _replace_last_activity(
                        in_progress.reviewer_summaries,
                        status="failed",
                        summary="Reviewer sandbox failed.",
                        details=str(exc),
                        active_sandbox_id=None,
                    ),
                }
            )
            return await self._coordinator.persist_progress(
                failed,
                runtime_status="Failed",
                status="Failed",
                phase="Failed",
                message="Reviewer sandbox failed.",
            )

        await self._cleanup_sandbox(sandbox)
        reviewed_tasks = list(in_progress.tasks)
        for index in review_task_indices:
            reviewed_tasks[index] = reviewed_tasks[index].model_copy(
                update={
                    "status": "Completed",
                    "active_sandbox_id": None,
                    "history": [*reviewed_tasks[index].history, SwarmTaskStatusTransition(status="Completed")],
                }
            )
        updated = in_progress.model_copy(
            update={
                "tasks": reviewed_tasks,
                "plan": in_progress.plan.model_copy(update={"tasks": reviewed_tasks}),
                "active_reviewer_sandbox_id": None,
                "publish_status": None,
                "publish_error": None,
                "target_branch": result.target_branch or in_progress.target_branch,
                "pull_request_url": result.pull_request_url or in_progress.pull_request_url,
                "pull_request_number": in_progress.pull_request_number,
                "total_execution_rounds": in_progress.total_execution_rounds + 1,
                "reviewer_summaries": _replace_last_activity(
                    in_progress.reviewer_summaries,
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
        )
        if result.outcome is ReviewOutcome.REPLAN:
            next_replan_count = updated.replan_count + 1
            if next_replan_count > updated.options.max_replans:
                capped = updated.model_copy(
                    update={
                        "replan_count": next_replan_count,
                        "consecutive_fix_rounds": 0,
                    }
                )
                return await self._coordinator.persist_progress(
                    capped,
                    runtime_status="Completed",
                    status="Completed",
                    phase="Completed",
                    message="Reviewer requested a replan, but the configured replan limit was reached. Completing best-effort without publish.",
                )
            replanned = updated.model_copy(
                update={
                    "replan_count": next_replan_count,
                    "consecutive_fix_rounds": 0,
                    "pending_replan_summary": result.replan_summary or result.summary,
                    "pending_replan_findings": result.replan_findings,
                    "plan": updated.plan.model_copy(update={"design_document": None, "tasks": []}),
                    "tasks": [],
                    "publish_status": None,
                    "publish_error": None,
                    "target_branch": None,
                    "pull_request_url": None,
                    "pull_request_number": None,
                }
            )
            return await self._coordinator.persist_progress(
                replanned,
                runtime_status="Running",
                status="Queued",
                phase="Planning",
                message="Reviewer requested a replan. The coordinator is returning to the planner with reviewer guidance.",
            )

        if result.outcome is ReviewOutcome.FIX_TASKS:
            next_fix_depth = updated.consecutive_fix_rounds + 1
            if next_fix_depth > updated.options.max_fix_chain_depth:
                capped = updated.model_copy(update={"consecutive_fix_rounds": next_fix_depth})
                return await self._coordinator.persist_progress(
                    capped,
                    runtime_status="Completed",
                    status="Completed",
                    phase="Completed",
                    message="Reviewer found additional fix work, but the configured fix-chain depth was reached. Completing best-effort without publish.",
                )
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
            queued = updated.model_copy(
                update={
                    "tasks": [*updated.tasks, *fix_tasks],
                    "plan": updated.plan.model_copy(update={"tasks": [*updated.tasks, *fix_tasks]}),
                    "consecutive_fix_rounds": next_fix_depth,
                    "publish_status": None,
                    "publish_error": None,
                    "target_branch": None,
                    "pull_request_url": None,
                    "pull_request_number": None,
                }
            )
            return await self._coordinator.persist_progress(
                queued,
                runtime_status="Running",
                status="Running",
                phase="Executing",
                message="Reviewer queued follow-up fix tasks before publish.",
            )

        remaining_ready = _next_ready_task_index(updated)
        if remaining_ready is not None:
            resumed = updated.model_copy(update={"consecutive_fix_rounds": 0})
            return await self._coordinator.persist_progress(
                resumed,
                runtime_status="Running",
                status="Running",
                phase="Executing",
                message="Reviewer approved the current execution wave. The coordinator is resuming queued tasks.",
            )

        completed = updated.model_copy(update={"consecutive_fix_rounds": 0})
        publish_result = await self._publish_run(completed)
        completed = completed.model_copy(
            update={
                "publish_status": publish_result.status,
                "publish_error": publish_result.error_message,
                "target_branch": publish_result.target_branch,
                "pull_request_url": publish_result.pull_request_url,
                "pull_request_number": publish_result.pull_request_number,
                "reviewer_summaries": _replace_last_activity(
                    completed.reviewer_summaries,
                    status="completed",
                    summary=_build_review_completion_summary(completed, publish_result),
                    details=_append_publish_details(completed.reviewer_summaries[-1].details, publish_result),
                    publish_status=publish_result.status,
                    publish_error=publish_result.error_message,
                    pull_request_url=publish_result.pull_request_url,
                    pull_request_number=publish_result.pull_request_number,
                ),
            }
        )
        return await self._coordinator.persist_progress(
            completed,
            runtime_status="Completed",
            status="Completed",
            phase="Completed",
            message=(
                _build_publish_completion_message(publish_result)
                if completed.target_branch
                else "Reviewer approved the completed run."
            ),
        )

    async def _terminate_run(self, run: SwarmRunState) -> SwarmRunState:
        tasks = [
            task.model_copy(
                update={
                    "status": task.status if task.is_completed else "Cancelled",
                    "active_sandbox_id": None,
                    "history": task.history if task.is_completed else [*task.history, SwarmTaskStatusTransition(status="Cancelled")],
                }
            )
            for task in run.tasks
        ]
        updated = run.model_copy(
            update={
                "runtime_status": "Terminated",
                "status": "Cancelled",
                "phase": "Cancelled",
                "message": "Run cancelled by request.",
                "active_planner_sandbox_id": None,
                "active_reviewer_sandbox_id": None,
                "tasks": tasks,
                "plan": run.plan.model_copy(update={"tasks": tasks}),
                "intent": run.intent.model_copy(update={"cancel_requested": False, "suspend_requested": False, "resume_requested": False}),
            }
        )
        return await self._coordinator.persist_progress(
            updated,
            runtime_status="Terminated",
            status="Cancelled",
            phase="Cancelled",
            message="Run cancelled by request.",
        )

    async def _suspend_run(self, run: SwarmRunState) -> SwarmRunState:
        updated = run.model_copy(
            update={
                "intent": run.intent.model_copy(update={"suspend_requested": False, "resume_requested": False}),
            }
        )
        return await self._coordinator.persist_progress(
            updated,
            runtime_status="Suspended",
            status="Suspended",
            phase="Suspended",
            message="Run suspended. Resume to continue durable execution.",
        )

    async def _create_role_sandbox(
        self,
        role: str,
        run: SwarmRunState,
        task: SwarmTaskState | None = None,
    ) -> SandboxHandle:
        request = self._sandbox_lifecycle.build_sandbox_request(role, run, task)
        request["environment"].update(await self._sandbox_lifecycle.build_execution_environment(run))
        sandbox_group = str(request.pop("sandbox_group"))
        resource_group = request.pop("resource_group", None)
        raw = await asyncio.to_thread(
            self._sandbox_client.create_sandbox,
            sandbox_group,
            resource_group=resource_group,
            **request,
        )
        return attach_sandbox_context(
            raw,
            sandbox_group=sandbox_group,
            resource_group=resource_group,
            default_resource_group=self._sandbox_lifecycle._settings.azure.resource_group,
        )

    async def _cleanup_sandbox(self, sandbox: SandboxHandle, *, failed: bool = False) -> None:
        with contextlib.suppress(Exception):
            await self._sandbox_lifecycle.cleanup_sandbox(sandbox, failed=failed)

    async def _publish_run(self, run: SwarmRunState) -> GitHubPublishResult:
        repo = parse_repository_context(run.repository_url, run.base_branch)
        target_branch = run.target_branch or build_integration_branch_name(run, repo)
        try:
            return await self._publish_service.publish_run(
                run,
                repo=repo,
                target_branch=target_branch,
            )
        except Exception as exc:
            return GitHubPublishResult(
                status="Failed",
                target_branch=target_branch,
                error_message=str(exc),
            )


def _next_ready_task_index(run: SwarmRunState) -> int | None:
    completed = {task.id for task in run.tasks if task.is_completed}
    for index, task in enumerate(run.tasks):
        if task.is_completed or task.status not in {"Pending", "Planned"}:
            continue
        if all(dependency in completed for dependency in task.dependencies):
            return index
    return None


def _pending_review_task_indices(run: SwarmRunState) -> list[int]:
    return [index for index, task in enumerate(run.tasks) if task.status == "PendingReview"]


def _should_continue(run: SwarmRunState) -> bool:
    return not run.is_terminal and not run.awaiting_plan_review and run.runtime_status != "Suspended"


def _replace_last_activity(
    activities: list[SwarmActivitySummary],
    *,
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
    updated = activities[-1].model_copy(
        update={
            "status": status,
            "summary": summary,
            "details": details if details is not None else activities[-1].details,
            "branch_name": branch_name if branch_name is not None else activities[-1].branch_name,
            "round_number": round_number if round_number is not None else activities[-1].round_number,
            "active_sandbox_id": (
                activities[-1].active_sandbox_id
                if active_sandbox_id is _UNSET
                else active_sandbox_id
            ),
            "findings": findings if findings is not None else activities[-1].findings,
            "fix_tasks": fix_tasks if fix_tasks is not None else activities[-1].fix_tasks,
            "replan_summary": (
                activities[-1].replan_summary
                if replan_summary is _UNSET
                else replan_summary
            ),
            "replan_findings": replan_findings if replan_findings is not None else activities[-1].replan_findings,
            "publish_status": activities[-1].publish_status if publish_status is _UNSET else publish_status,
            "publish_error": activities[-1].publish_error if publish_error is _UNSET else publish_error,
            "pull_request_url": activities[-1].pull_request_url if pull_request_url is _UNSET else pull_request_url,
            "pull_request_number": (
                activities[-1].pull_request_number
                if pull_request_number is _UNSET
                else pull_request_number
            ),
            "head_commit_sha": activities[-1].head_commit_sha if head_commit_sha is _UNSET else head_commit_sha,
            "parent_commit_sha": (
                activities[-1].parent_commit_sha if parent_commit_sha is _UNSET else parent_commit_sha
            ),
            "changed_files": changed_files if changed_files is not None else activities[-1].changed_files,
            "validation_summary": (
                activities[-1].validation_summary if validation_summary is _UNSET else validation_summary
            ),
            "validation_results": (
                validation_results if validation_results is not None else activities[-1].validation_results
            ),
        }
    )
    return [*activities[:-1], updated]


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


def _derive_title(prompt: str) -> str:
    normalized = " ".join(prompt.split())
    return normalized[:77] + "..." if len(normalized) > 80 else normalized
