from __future__ import annotations

import hashlib
import json

from agent_swarm_service.api.schemas import (
    SwarmActivitySummaryResponse,
    SwarmMergeResolverSandboxResponse,
    SwarmReviewFindingResponse,
    SwarmReviewFixTaskResponse,
    SwarmRunAgentSettingResponse,
    SwarmRunAgentSettingsResponse,
    SwarmRunBranchStateResponse,
    SwarmRunDetailsResponse,
    SwarmRunEventSnapshotResponse,
    SwarmRunPlanResponse,
    SwarmRunPlanningSettingsResponse,
    SwarmRunSummaryResponse,
    SwarmRunTasksResponse,
    SwarmTaskResponse,
    SwarmTaskStatusTransitionResponse,
    SwarmValidationCommandResultResponse,
)
from agent_swarm_service.orchestration.models import RunProjectionSnapshot, SwarmRunState


def build_summary_response(run: SwarmRunState) -> SwarmRunSummaryResponse:
    return SwarmRunSummaryResponse(
        id=run.id,
        title=run.title,
        prompt=run.prompt,
        repository_url=run.repository_url,
        runtime_status=run.runtime_status,
        status=run.status,
        phase=run.phase,
        message=run.message,
        failure_message=run.failure_message,
        created_at_utc=run.created_at_utc,
        last_updated_at_utc=run.last_updated_at_utc,
        can_cancel=run.can_cancel,
        can_suspend=run.can_suspend,
        can_resume=run.can_resume,
        can_purge=run.can_purge,
        task_count=run.task_count,
        completed_task_count=run.completed_task_count,
        active_planner_sandbox_id=run.active_planner_sandbox_id,
        active_reviewer_sandbox_id=run.active_reviewer_sandbox_id,
    )


def build_plan_response(run: SwarmRunState) -> SwarmRunPlanResponse:
    return SwarmRunPlanResponse(
        id=run.id,
        title=run.title,
        runtime_status=run.runtime_status,
        status=run.status,
        phase=run.phase,
        awaiting_plan_review=run.awaiting_plan_review,
        planning_settings=_to_planning_settings(run),
        design_document=run.plan.design_document,
        tasks=[_to_task_response(task) for task in run.plan.tasks or run.tasks],
        active_planner_sandbox_id=run.active_planner_sandbox_id,
        active_reviewer_sandbox_id=run.active_reviewer_sandbox_id,
    )


def build_tasks_response(run: SwarmRunState) -> SwarmRunTasksResponse:
    return SwarmRunTasksResponse(
        id=run.id,
        runtime_status=run.runtime_status,
        status=run.status,
        phase=run.phase,
        tasks=[_to_task_response(task) for task in run.tasks],
        active_planner_sandbox_id=run.active_planner_sandbox_id,
        active_reviewer_sandbox_id=run.active_reviewer_sandbox_id,
    )


def build_details_response(run: SwarmRunState) -> SwarmRunDetailsResponse:
    return SwarmRunDetailsResponse(
        id=run.id,
        title=run.title,
        prompt=run.prompt,
        repository_url=run.repository_url,
        base_branch=run.base_branch,
        target_branch=run.target_branch,
        runtime_status=run.runtime_status,
        status=run.status,
        phase=run.phase,
        message=run.message,
        failure_message=run.failure_message,
        created_at_utc=run.created_at_utc,
        last_updated_at_utc=run.last_updated_at_utc,
        can_cancel=run.can_cancel,
        can_suspend=run.can_suspend,
        can_resume=run.can_resume,
        can_rerun=run.can_rerun,
        can_purge=run.can_purge,
        awaiting_plan_review=run.awaiting_plan_review,
        planning_settings=_to_planning_settings(run),
        task_count=run.task_count,
        completed_task_count=run.completed_task_count,
        design_document=run.plan.design_document,
        branch_state=SwarmRunBranchStateResponse.model_validate(run.branch_state.model_dump(mode="json")),
        publish_status=run.publish_status,
        publish_error=run.publish_error,
        pull_request_url=run.pull_request_url,
        pull_request_number=run.pull_request_number,
        agent_settings=SwarmRunAgentSettingsResponse(
            planner=SwarmRunAgentSettingResponse(
                model=run.options.models.planner.model,
            ),
            worker=SwarmRunAgentSettingResponse(
                model=run.options.models.worker.model,
            ),
            reviewer=SwarmRunAgentSettingResponse(
                model=run.options.models.reviewer.model,
            ),
        ),
        tasks=[_to_task_response(task) for task in run.tasks],
        merge_resolver_sandboxes=[
            SwarmMergeResolverSandboxResponse(
                branch_name=item.branch_name,
                round_number=item.round_number,
                sandbox_id=item.sandbox_id,
            )
            for item in run.merge_resolver_sandboxes
        ],
        planner_summaries=[_to_activity_summary(item) for item in run.planner_summaries],
        worker_summaries=[_to_activity_summary(item) for item in run.worker_summaries],
        reviewer_summaries=[_to_activity_summary(item) for item in run.reviewer_summaries],
        active_planner_sandbox_id=run.active_planner_sandbox_id,
        active_reviewer_sandbox_id=run.active_reviewer_sandbox_id,
    )


def build_event_snapshot(run: SwarmRunState) -> SwarmRunEventSnapshotResponse:
    return SwarmRunEventSnapshotResponse(
        summary=build_summary_response(run),
        tasks=build_tasks_response(run),
        plan=build_plan_response(run),
        details=build_details_response(run),
        is_terminal=run.is_terminal,
    )


def build_projection_snapshot(run: SwarmRunState) -> RunProjectionSnapshot:
    snapshot = build_event_snapshot(run)
    summary = snapshot.summary.model_dump(by_alias=True, exclude_none=True, mode="json")
    plan = snapshot.plan.model_dump(by_alias=True, exclude_none=True, mode="json")
    tasks = snapshot.tasks.model_dump(by_alias=True, exclude_none=True, mode="json")
    details = snapshot.details.model_dump(by_alias=True, exclude_none=True, mode="json")
    payload = {
        "summary": summary,
        "plan": plan,
        "tasks": tasks,
        "details": details,
        "isTerminal": snapshot.is_terminal,
    }
    checksum = hashlib.sha256(json.dumps(payload, sort_keys=True).encode("utf-8")).hexdigest()
    return RunProjectionSnapshot(
        run_id=run.id,
        owner_user_id=run.owner.user_id,
        sequence=run.checkpoint.sequence if run.checkpoint else 0,
        checksum=checksum,
        summary=summary,
        plan=plan,
        tasks=tasks,
        details=details,
        is_terminal=snapshot.is_terminal,
        updated_at_utc=run.last_updated_at_utc,
    )


def hydrate_event_snapshot(projection: RunProjectionSnapshot) -> SwarmRunEventSnapshotResponse:
    return SwarmRunEventSnapshotResponse(
        summary=SwarmRunSummaryResponse.model_validate(projection.summary),
        tasks=SwarmRunTasksResponse.model_validate(projection.tasks),
        plan=SwarmRunPlanResponse.model_validate(projection.plan),
        details=SwarmRunDetailsResponse.model_validate(projection.details),
        is_terminal=projection.is_terminal,
    )


def _to_activity_summary(item) -> SwarmActivitySummaryResponse:
    return SwarmActivitySummaryResponse(
        id=item.id,
        kind=item.kind,
        title=item.title,
        status=item.status,
        summary=item.summary,
        details=item.details,
        assignee=item.assignee,
        branch_name=item.branch_name,
        round_number=item.round_number,
        active_sandbox_id=item.active_sandbox_id,
        findings=[
            SwarmReviewFindingResponse(
                task_id=finding.task_id,
                severity=finding.severity,
                description=finding.description,
            )
            for finding in item.findings
        ],
        fix_tasks=[
            SwarmReviewFixTaskResponse(
                id=task.id,
                title=task.title,
                description=task.description,
                dependencies=task.dependencies,
                round_number=task.round_number,
                branch_name=task.branch_name,
            )
            for task in item.fix_tasks
        ],
        replan_summary=item.replan_summary,
        replan_findings=item.replan_findings,
        publish_status=item.publish_status,
        publish_error=item.publish_error,
        pull_request_url=item.pull_request_url,
        pull_request_number=item.pull_request_number,
        head_commit_sha=item.head_commit_sha,
        parent_commit_sha=item.parent_commit_sha,
        changed_files=item.changed_files,
        validation_summary=item.validation_summary,
        validation_results=[
            SwarmValidationCommandResultResponse.model_validate(result.model_dump(mode="json"))
            for result in item.validation_results
        ],
    )


def _to_task_response(task) -> SwarmTaskResponse:
    return SwarmTaskResponse(
        id=task.id,
        title=task.title,
        status=task.status,
        summary=task.summary,
        assignee=task.assignee,
        branch_name=task.branch_name,
        round_number=task.round_number,
        is_completed=task.is_completed,
        dependencies=task.dependencies,
        history=[
            SwarmTaskStatusTransitionResponse(
                status=transition.status,
                timestamp=transition.timestamp,
            )
            for transition in task.history
        ],
        failure_details=task.failure_details,
        active_sandbox_id=task.active_sandbox_id,
        head_commit_sha=task.head_commit_sha,
        parent_commit_sha=task.parent_commit_sha,
        changed_files=task.changed_files,
        validation_summary=task.validation_summary,
        validation_results=[
            SwarmValidationCommandResultResponse.model_validate(result.model_dump(mode="json"))
            for result in task.validation_results
        ],
    )


def _to_planning_settings(run: SwarmRunState) -> SwarmRunPlanningSettingsResponse:
    return SwarmRunPlanningSettingsResponse(
        human_review_mode=run.options.planning.human_review_mode,
        plan_review_timeout_hours=run.options.planning.plan_review_timeout_hours,
    )
