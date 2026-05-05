from __future__ import annotations

import asyncio

from fastapi import APIRouter, Depends, HTTPException, Query, status
from fastapi.encoders import jsonable_encoder
from fastapi.responses import StreamingResponse

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
from agent_swarm_service.api.sse import format_sse_event
from agent_swarm_service.dependencies import (
    get_settings,
    get_sandbox_client,
    get_swarm_run_service,
    read_log_chunk,
)
from agent_swarm_service.sandboxes.workspace import DEFAULT_LOG_MIRROR_PATH

router = APIRouter(prefix="/api/swarm-runs", tags=["swarm-runs"])


@router.get("", response_model=list[SwarmRunSummaryResponse], response_model_exclude_none=True)
async def list_runs(
    swarm_run_service=Depends(get_swarm_run_service),
) -> list[SwarmRunSummaryResponse]:
    return await swarm_run_service.list_runs()


@router.post(
    "",
    response_model=SwarmRunSummaryResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
async def create_run(
    request: CreateSwarmRunRequest,
    swarm_run_service=Depends(get_swarm_run_service),
) -> SwarmRunSummaryResponse:
    return await swarm_run_service.create_run(request)


@router.get("/{run_id}", response_model=SwarmRunSummaryResponse, response_model_exclude_none=True)
async def get_run(
    run_id: str,
    swarm_run_service=Depends(get_swarm_run_service),
) -> SwarmRunSummaryResponse:
    response = await swarm_run_service.get_run(run_id)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return response


@router.get("/{run_id}/plan", response_model=SwarmRunPlanResponse, response_model_exclude_none=True)
async def get_plan(
    run_id: str,
    swarm_run_service=Depends(get_swarm_run_service),
) -> SwarmRunPlanResponse:
    response = await swarm_run_service.get_plan(run_id)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return response


@router.post("/{run_id}/plan/feedback", status_code=status.HTTP_202_ACCEPTED)
async def submit_plan_feedback(
    run_id: str,
    request: SwarmPlanFeedbackRequest,
    swarm_run_service=Depends(get_swarm_run_service),
) -> None:
    response = await swarm_run_service.submit_plan_feedback(run_id, request)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")


@router.get("/{run_id}/tasks", response_model=SwarmRunTasksResponse, response_model_exclude_none=True)
async def get_tasks(
    run_id: str,
    swarm_run_service=Depends(get_swarm_run_service),
) -> SwarmRunTasksResponse:
    response = await swarm_run_service.get_tasks(run_id)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return response


@router.get("/{run_id}/details", response_model=SwarmRunDetailsResponse, response_model_exclude_none=True)
async def get_details(
    run_id: str,
    swarm_run_service=Depends(get_swarm_run_service),
) -> SwarmRunDetailsResponse:
    response = await swarm_run_service.get_details(run_id)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return response


@router.delete("/{run_id}", response_model=SwarmRunCancelResponse)
async def cancel(
    run_id: str,
    swarm_run_service=Depends(get_swarm_run_service),
) -> SwarmRunCancelResponse:
    response = await swarm_run_service.cancel(run_id)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return response


@router.post("/{run_id}/suspend", response_model=SwarmRunSuspendResponse)
async def suspend(
    run_id: str,
    reason: str | None = Query(default=None),
    swarm_run_service=Depends(get_swarm_run_service),
) -> SwarmRunSuspendResponse:
    response = await swarm_run_service.suspend(run_id, reason)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return response


@router.post("/{run_id}/resume", response_model=SwarmRunResumeResponse)
async def resume(
    run_id: str,
    reason: str | None = Query(default=None),
    swarm_run_service=Depends(get_swarm_run_service),
) -> SwarmRunResumeResponse:
    response = await swarm_run_service.resume(run_id, reason)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return response


@router.delete("/{run_id}/purge", response_model=SwarmRunPurgeResponse)
async def purge(
    run_id: str,
    swarm_run_service=Depends(get_swarm_run_service),
) -> SwarmRunPurgeResponse:
    response = await swarm_run_service.purge(run_id)
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return response


@router.post(
    "/{run_id}/rerun",
    response_model=SwarmRunSummaryResponse,
    response_model_exclude_none=True,
    status_code=status.HTTP_201_CREATED,
)
async def rerun(
    run_id: str,
    swarm_run_service=Depends(get_swarm_run_service),
) -> SwarmRunSummaryResponse:
    try:
        response = await swarm_run_service.rerun(run_id)
    except RuntimeError as exc:
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    if response is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Run not found.")
    return response


@router.get("/{run_id}/events")
async def stream_events(
    run_id: str,
    swarm_run_service=Depends(get_swarm_run_service),
) -> StreamingResponse:
    async def event_stream():
        snapshot = await swarm_run_service.get_event_snapshot(run_id)
        if snapshot is None:
            yield format_sse_event("error", {"error": "not_found"})
            return

        last_payloads: dict[str, object] = {}
        for event_type, payload in _snapshot_payloads(snapshot).items():
            last_payloads[event_type] = payload
            yield format_sse_event(event_type, payload)

        if snapshot.is_terminal:
            yield format_sse_event("done", {"done": True})
            return

        while True:
            await asyncio.sleep(1)
            snapshot = await swarm_run_service.get_event_snapshot(run_id)
            if snapshot is None:
                yield format_sse_event("error", {"error": "not_found"})
                return
            for event_type, payload in _snapshot_payloads(snapshot).items():
                if last_payloads.get(event_type) != payload:
                    last_payloads[event_type] = payload
                    yield format_sse_event(event_type, payload)
            if snapshot.is_terminal:
                yield format_sse_event("done", {"done": True})
                return

    return StreamingResponse(event_stream(), media_type="text/event-stream")


@router.get("/{run_id}/sandboxes/{sandbox_id}/logstream")
async def stream_sandbox_logs(
    run_id: str,
    sandbox_id: str,
    settings=Depends(get_settings),
    swarm_run_service=Depends(get_swarm_run_service),
    sandbox_client=Depends(get_sandbox_client),
) -> StreamingResponse:
    snapshot = await swarm_run_service.get_event_snapshot(run_id)
    if not _can_stream_active_sandbox(snapshot, sandbox_id):
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Sandbox log stream not found.")

    async def log_stream():
        offset = 0
        while True:
            snapshot = await swarm_run_service.get_event_snapshot(run_id)
            if not _can_stream_active_sandbox(snapshot, sandbox_id):
                return
            chunk = await read_log_chunk(
                sandbox_id,
                settings.azure.sandbox_group_name,
                DEFAULT_LOG_MIRROR_PATH,
                offset=offset,
                limit_bytes=8192,
                sandbox_client=sandbox_client,
            )
            offset = chunk.offset
            if chunk.content:
                yield chunk.content.encode("utf-8")
            await asyncio.sleep(1)

    return StreamingResponse(log_stream(), media_type="text/plain; charset=utf-8")


def _snapshot_payloads(snapshot: SwarmRunEventSnapshotResponse) -> dict[str, object]:
    return {
        "status": jsonable_encoder(snapshot.summary, by_alias=True, exclude_none=True),
        "tasks": jsonable_encoder(snapshot.tasks, by_alias=True, exclude_none=True),
        "plan": jsonable_encoder(snapshot.plan, by_alias=True, exclude_none=True),
        "details": jsonable_encoder(snapshot.details, by_alias=True, exclude_none=True),
    }


def _has_active_sandbox(snapshot: SwarmRunEventSnapshotResponse, sandbox_id: str) -> bool:
    if not sandbox_id:
        return False
    if snapshot.summary.active_planner_sandbox_id == sandbox_id:
        return True
    if snapshot.summary.active_reviewer_sandbox_id == sandbox_id:
        return True
    if any(task.active_sandbox_id == sandbox_id for task in snapshot.tasks.tasks):
        return True
    return any(item.sandbox_id == sandbox_id for item in snapshot.details.merge_resolver_sandboxes)


def _can_stream_active_sandbox(
    snapshot: SwarmRunEventSnapshotResponse | None,
    sandbox_id: str,
) -> bool:
    return snapshot is not None and not snapshot.is_terminal and _has_active_sandbox(snapshot, sandbox_id)
