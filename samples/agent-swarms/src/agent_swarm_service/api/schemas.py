from __future__ import annotations

from datetime import datetime
from typing import Any

from pydantic import BaseModel, ConfigDict, Field, SecretStr, field_validator, model_validator

from agent_swarm_service.orchestration.models import (
    HumanReviewMode,
    PlanFeedbackAction,
    PlanTaskRevision,
    SANDBOX_SELECTOR_FIELDS,
    SwarmOptions,
    validate_sandbox_selector_values,
)


def to_camel(value: str) -> str:
    head, *tail = value.split("_")
    return head + "".join(part.capitalize() for part in tail)


class CamelModel(BaseModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True)


class HealthResponse(CamelModel):
    status: str
    service: str
    version: str


class CreateSwarmRunOptions(CamelModel):
    model_config = ConfigDict(alias_generator=to_camel, populate_by_name=True, extra="forbid")

    sandbox_disk_id: str | None = None
    planner_model: str | None = None
    worker_model: str | None = None
    reviewer_model: str | None = None
    human_review_mode: HumanReviewMode | None = None
    plan_review_timeout_hours: int | None = Field(default=None, ge=1, le=24)

    @field_validator(
        "planner_model",
        "worker_model",
        "reviewer_model",
        "sandbox_disk_id",
        mode="before",
    )
    @classmethod
    def normalize_optional_text(cls, value: Any) -> str | None:
        if value is None:
            return None
        text = str(value).strip()
        return text or None

    @model_validator(mode="after")
    def validate_sandbox_selectors(self) -> "CreateSwarmRunOptions":
        validate_sandbox_selector_values(
            sandbox_disk_id=self.sandbox_disk_id,
            layer_name="create-run options",
        )
        return self

    def apply_to(self, defaults: SwarmOptions) -> SwarmOptions:
        sandbox_data = defaults.sandbox.model_dump(mode="python")
        selector_updates = {
            field_name: getattr(self, field_name, None)
            for field_name in SANDBOX_SELECTOR_FIELDS
            if getattr(self, field_name, None) is not None
        }
        if selector_updates:
            for field_name in SANDBOX_SELECTOR_FIELDS:
                sandbox_data[field_name] = None
            sandbox_data.update(selector_updates)
        validate_sandbox_selector_values(
            sandbox_disk_id=sandbox_data.get("sandbox_disk_id"),
            layer_name="resolved sandbox settings",
        )
        sandbox = defaults.sandbox.__class__.model_validate(sandbox_data)
        return defaults.model_copy(
            update={
                "planning": defaults.planning.model_copy(
                    update={
                        "human_review_mode": self.human_review_mode
                        if self.human_review_mode is not None
                        else defaults.planning.human_review_mode,
                        "plan_review_timeout_hours": self.plan_review_timeout_hours
                        if self.plan_review_timeout_hours is not None
                        else defaults.planning.plan_review_timeout_hours,
                    }
                ),
                "models": defaults.models.model_copy(
                    update={
                        "planner": defaults.models.planner.model_copy(
                            update={"model": self.planner_model or defaults.models.planner.model}
                        ),
                        "worker": defaults.models.worker.model_copy(
                            update={"model": self.worker_model or defaults.models.worker.model}
                        ),
                        "reviewer": defaults.models.reviewer.model_copy(
                            update={"model": self.reviewer_model or defaults.models.reviewer.model}
                        ),
                    }
                ),
                "sandbox": sandbox,
            }
        )


class CreateSwarmRunRequest(CamelModel):
    prompt: str = Field(min_length=1)
    repository_url: str = Field(min_length=1)
    github_pat: SecretStr
    base_branch: str | None = None
    options: CreateSwarmRunOptions | None = None

    @field_validator("github_pat", mode="before")
    @classmethod
    def normalize_github_pat(cls, value: Any) -> SecretStr:
        if value is None:
            raise ValueError("GitHub PAT is required.")
        text = str(value).strip()
        if not text:
            raise ValueError("GitHub PAT is required.")
        return SecretStr(text)


class SwarmTaskStatusTransitionResponse(CamelModel):
    status: str
    timestamp: datetime


class SwarmValidationCommandResultResponse(CamelModel):
    command: str
    exit_code: int
    status: str
    stdout: str | None = None
    stderr: str | None = None


class SwarmTaskResponse(CamelModel):
    id: str
    title: str
    status: str
    summary: str | None = None
    assignee: str | None = None
    branch_name: str | None = None
    round_number: int | None = None
    is_completed: bool
    dependencies: list[str] = Field(default_factory=list)
    history: list[SwarmTaskStatusTransitionResponse] = Field(default_factory=list)
    failure_details: str | None = None
    active_sandbox_id: str | None = None
    head_commit_sha: str | None = None
    parent_commit_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    validation_summary: str | None = None
    validation_results: list[SwarmValidationCommandResultResponse] = Field(default_factory=list)


class SwarmRunAgentSettingResponse(CamelModel):
    model: str


class SwarmRunAgentSettingsResponse(CamelModel):
    planner: SwarmRunAgentSettingResponse
    worker: SwarmRunAgentSettingResponse
    reviewer: SwarmRunAgentSettingResponse


class SwarmRunPlanningSettingsResponse(CamelModel):
    human_review_mode: HumanReviewMode
    plan_review_timeout_hours: int


class SwarmActivitySummaryResponse(CamelModel):
    id: str
    kind: str
    title: str
    status: str
    summary: str | None = None
    details: str | None = None
    assignee: str | None = None
    branch_name: str | None = None
    round_number: int | None = None
    active_sandbox_id: str | None = None
    findings: list["SwarmReviewFindingResponse"] = Field(default_factory=list)
    fix_tasks: list["SwarmReviewFixTaskResponse"] = Field(default_factory=list)
    replan_summary: str | None = None
    replan_findings: list[str] = Field(default_factory=list)
    publish_status: str | None = None
    publish_error: str | None = None
    pull_request_url: str | None = None
    pull_request_number: int | None = None
    head_commit_sha: str | None = None
    parent_commit_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    validation_summary: str | None = None
    validation_results: list[SwarmValidationCommandResultResponse] = Field(default_factory=list)


class SwarmReviewFindingResponse(CamelModel):
    task_id: str
    severity: str
    description: str


class SwarmReviewFixTaskResponse(CamelModel):
    id: str
    title: str
    description: str
    dependencies: list[str] = Field(default_factory=list)
    round_number: int | None = None
    branch_name: str | None = None


class SwarmMergeResolverSandboxResponse(CamelModel):
    branch_name: str
    round_number: int
    sandbox_id: str


class SwarmBranchHeadStateResponse(CamelModel):
    branch_name: str | None = None
    checkpoint_sequence: int | None = None
    commit_sha: str | None = None
    reference_type: str


class SwarmRunMergeStateResponse(CamelModel):
    status: str
    resolution_state: str
    active_resolution_sandbox_id: str | None = None
    has_unreviewed_changes: bool
    has_unapproved_changes: bool


class SwarmRunBranchStateResponse(CamelModel):
    branch_name: str | None = None
    current_head_sha: str | None = None
    current_head_checkpoint_sequence: int | None = None
    reviewed_head_sha: str | None = None
    reviewed_checkpoint_sequence: int | None = None
    approved_branch_name: str | None = None
    approved_head_sha: str | None = None
    approved_checkpoint_sequence: int | None = None
    active_wave: int
    current_wave_round: int
    current_head: SwarmBranchHeadStateResponse
    reviewed_head: SwarmBranchHeadStateResponse
    approved_head: SwarmBranchHeadStateResponse
    merge_status: str
    merge_resolution_state: str
    merge_resolution_sandbox_id: str | None = None
    merge_state: SwarmRunMergeStateResponse


class SwarmRunSummaryResponse(CamelModel):
    id: str
    title: str | None = None
    prompt: str
    repository_url: str
    runtime_status: str
    status: str
    phase: str
    message: str | None = None
    failure_message: str | None = None
    created_at_utc: datetime
    last_updated_at_utc: datetime
    can_cancel: bool
    can_suspend: bool
    can_resume: bool
    can_purge: bool
    task_count: int
    completed_task_count: int
    active_planner_sandbox_id: str | None = None
    active_reviewer_sandbox_id: str | None = None


class SwarmRunPlanResponse(CamelModel):
    id: str
    title: str | None = None
    runtime_status: str
    status: str
    phase: str
    awaiting_plan_review: bool
    planning_settings: SwarmRunPlanningSettingsResponse
    design_document: str | None = None
    tasks: list[SwarmTaskResponse] = Field(default_factory=list)
    active_planner_sandbox_id: str | None = None
    active_reviewer_sandbox_id: str | None = None


class SwarmRunTasksResponse(CamelModel):
    id: str
    runtime_status: str
    status: str
    phase: str
    tasks: list[SwarmTaskResponse] = Field(default_factory=list)
    active_planner_sandbox_id: str | None = None
    active_reviewer_sandbox_id: str | None = None


class SwarmRunDetailsResponse(CamelModel):
    id: str
    title: str | None = None
    prompt: str
    repository_url: str
    base_branch: str | None = None
    target_branch: str | None = None
    runtime_status: str
    status: str
    phase: str
    message: str | None = None
    failure_message: str | None = None
    created_at_utc: datetime
    last_updated_at_utc: datetime
    can_cancel: bool
    can_suspend: bool
    can_resume: bool
    can_rerun: bool
    can_purge: bool
    awaiting_plan_review: bool
    planning_settings: SwarmRunPlanningSettingsResponse
    task_count: int
    completed_task_count: int
    design_document: str | None = None
    branch_state: SwarmRunBranchStateResponse
    publish_status: str | None = None
    publish_error: str | None = None
    pull_request_url: str | None = None
    pull_request_number: int | None = None
    agent_settings: SwarmRunAgentSettingsResponse
    tasks: list[SwarmTaskResponse] = Field(default_factory=list)
    merge_resolver_sandboxes: list[SwarmMergeResolverSandboxResponse] = Field(default_factory=list)
    planner_summaries: list[SwarmActivitySummaryResponse] = Field(default_factory=list)
    worker_summaries: list[SwarmActivitySummaryResponse] = Field(default_factory=list)
    reviewer_summaries: list[SwarmActivitySummaryResponse] = Field(default_factory=list)
    active_planner_sandbox_id: str | None = None
    active_reviewer_sandbox_id: str | None = None


class SwarmRunCancelResponse(CamelModel):
    id: str
    status: str


class SwarmRunSuspendResponse(CamelModel):
    id: str
    status: str


class SwarmRunResumeResponse(CamelModel):
    id: str
    status: str


class SwarmRunPurgeResponse(CamelModel):
    id: str


class SwarmRunEventSnapshotResponse(CamelModel):
    summary: SwarmRunSummaryResponse
    tasks: SwarmRunTasksResponse
    plan: SwarmRunPlanResponse | None = None
    details: SwarmRunDetailsResponse
    is_terminal: bool


class SwarmTaskFeedbackItem(CamelModel):
    id: str
    title: str
    description: str | None = None

    def to_revision(self) -> PlanTaskRevision:
        return PlanTaskRevision(id=self.id, title=self.title, description=self.description)


class SwarmPlanFeedbackRequest(CamelModel):
    action: PlanFeedbackAction
    comments: str | None = None
    revised_tasks: list[SwarmTaskFeedbackItem] = Field(default_factory=list)
