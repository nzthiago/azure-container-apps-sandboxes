from __future__ import annotations

from datetime import UTC, datetime, timedelta

from agent_swarm_service.api.schemas import (
    CreateSwarmRunRequest,
    SwarmPlanFeedbackRequest,
    SwarmRunCancelResponse,
    SwarmRunDetailsResponse,
    SwarmRunEventSnapshotResponse,
    SwarmRunPlanResponse,
    SwarmRunPurgeResponse,
    SwarmRunResumeResponse,
    SwarmRunSummaryResponse,
    SwarmRunSuspendResponse,
    SwarmRunTasksResponse,
)
from agent_swarm_service.auth.session_store import RunSecretStore, build_run_secret
from agent_swarm_service.config import ServiceSettings
from agent_swarm_service.orchestration.coordinator import SwarmRunCoordinator
from agent_swarm_service.orchestration.models import PlanFeedbackSubmission, RunOwner
from agent_swarm_service.orchestration.projections import (
    build_details_response,
    build_event_snapshot,
    build_plan_response,
    build_summary_response,
    build_tasks_response,
    hydrate_event_snapshot,
)


class SwarmRunService:
    def __init__(
        self,
        settings: ServiceSettings,
        coordinator: SwarmRunCoordinator,
        run_secret_store: RunSecretStore,
    ) -> None:
        self._settings = settings
        self._coordinator = coordinator
        self._run_secret_store = run_secret_store

    async def list_runs(self) -> list[SwarmRunSummaryResponse]:
        runs = await self._coordinator.list_runs()
        return [build_summary_response(run) for run in runs]

    async def create_run(self, request: CreateSwarmRunRequest) -> SwarmRunSummaryResponse:
        defaults = self._settings.runtime.to_swarm_options()
        resolved_options = request.options.apply_to(defaults) if request.options else defaults
        if resolved_options.sandbox.sandbox_disk_id is None:
            raise ValueError(
                "A private sandbox DiskId is required. Set SWARM_SANDBOX_DISK_ID or provide "
                "options.sandboxDiskId on the run request."
            )
        run_id = self._new_run_id()
        try:
            await self._run_secret_store.store(
                build_run_secret(
                    run_id,
                    request.github_pat.get_secret_value(),
                    lifetime=timedelta(hours=self._settings.app.run_token_lifetime_hours),
                )
            )
            run = await self._coordinator.create_run(
                owner=self._to_owner(run_id),
                prompt=request.prompt,
                repository_url=request.repository_url,
                base_branch=request.base_branch,
                options=resolved_options,
                run_id=run_id,
            )
        except Exception:
            await self._run_secret_store.delete(run_id)
            raise
        return build_summary_response(run)

    async def get_run(self, run_id: str) -> SwarmRunSummaryResponse | None:
        run = await self._coordinator.get_run(run_id)
        return None if run is None else build_summary_response(run)

    async def get_plan(self, run_id: str) -> SwarmRunPlanResponse | None:
        run = await self._coordinator.get_run(run_id)
        return None if run is None else build_plan_response(run)

    async def submit_plan_feedback(
        self,
        run_id: str,
        request: SwarmPlanFeedbackRequest,
    ) -> bool | None:
        run = await self._coordinator.submit_plan_feedback(
            run_id,
            PlanFeedbackSubmission(
                action=request.action,
                comments=request.comments,
                revised_tasks=[task.to_revision() for task in request.revised_tasks],
            ),
        )
        return None if run is None else True

    async def get_tasks(self, run_id: str) -> SwarmRunTasksResponse | None:
        run = await self._coordinator.get_run(run_id)
        return None if run is None else build_tasks_response(run)

    async def get_details(self, run_id: str) -> SwarmRunDetailsResponse | None:
        run = await self._coordinator.get_run(run_id)
        return None if run is None else build_details_response(run)

    async def cancel(self, run_id: str) -> SwarmRunCancelResponse | None:
        run = await self._coordinator.request_cancel(run_id)
        return None if run is None else SwarmRunCancelResponse(id=run.id, status=run.status)

    async def suspend(
        self,
        run_id: str,
        reason: str | None,
    ) -> SwarmRunSuspendResponse | None:
        run = await self._coordinator.request_suspend(run_id, reason)
        return None if run is None else SwarmRunSuspendResponse(id=run.id, status=run.status)

    async def resume(
        self,
        run_id: str,
        reason: str | None,
    ) -> SwarmRunResumeResponse | None:
        run = await self._coordinator.request_resume(run_id, reason)
        return None if run is None else SwarmRunResumeResponse(id=run.id, status=run.status)

    async def purge(self, run_id: str) -> SwarmRunPurgeResponse | None:
        removed = await self._coordinator.purge_run(run_id)
        if not removed:
            return None
        await self._run_secret_store.delete(run_id)
        return SwarmRunPurgeResponse(id=run_id)

    async def rerun(self, run_id: str) -> SwarmRunSummaryResponse | None:
        existing_secret = await self._run_secret_store.get(run_id)
        if existing_secret is None:
            raise RuntimeError("The run-scoped GitHub token has expired. Create a new run with a fresh PAT.")
        rerun_id = self._new_run_id()
        try:
            await self._run_secret_store.store(
                build_run_secret(
                    rerun_id,
                    existing_secret.token.get_secret_value(),
                    lifetime=timedelta(hours=self._settings.app.run_token_lifetime_hours),
                )
            )
            run = await self._coordinator.rerun(
                run_id,
                self._to_owner(rerun_id),
                new_run_id=rerun_id,
            )
            if run is None:
                await self._run_secret_store.delete(rerun_id)
                return None
        except Exception:
            await self._run_secret_store.delete(rerun_id)
            raise
        return build_summary_response(run)

    async def get_event_snapshot(self, run_id: str) -> SwarmRunEventSnapshotResponse | None:
        projection = await self._coordinator.get_projection(run_id)
        if projection is not None:
            return hydrate_event_snapshot(projection)
        run = await self._coordinator.get_run(run_id)
        if run is None:
            return None
        return build_event_snapshot(run)

    @staticmethod
    def _to_owner(run_id: str) -> RunOwner:
        return RunOwner(session_id=run_id)

    @staticmethod
    def _new_run_id() -> str:
        return datetime.now(UTC).strftime("%Y%m%d%H%M%S%f")
