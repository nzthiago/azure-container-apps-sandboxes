from __future__ import annotations

from datetime import UTC, datetime
from enum import Enum
from typing import Any

from pydantic import BaseModel, Field, computed_field, field_validator, model_validator

SANDBOX_SELECTOR_FIELDS = (
    "sandbox_disk_id",
)


def normalize_optional_text(value: Any) -> str | None:
    if value is None:
        return None
    text = str(value).strip()
    return text or None


def validate_sandbox_selector_values(
    *,
    sandbox_disk_id: str | None,
    layer_name: str,
) -> None:
    del layer_name
    if sandbox_disk_id is None:
        return


def utcnow() -> datetime:
    return datetime.now(UTC)


class HumanReviewMode(str, Enum):
    NONE = "None"
    REQUIRED = "Required"


class PlanFeedbackAction(str, Enum):
    APPROVED = "Approved"
    REQUEST_CHANGES = "RequestChanges"


class ReviewOutcome(str, Enum):
    APPROVED = "Approved"
    FIX_TASKS = "FixTasks"
    REPLAN = "Replan"


class ModelSelection(BaseModel):
    model: str


class CopilotRuntimeSettings(BaseModel):
    provider: str = "github-copilot-sdk"
    token_environment_variable: str = "GH_TOKEN"
    api_base_url: str = "https://models.github.ai/inference"


class SwarmAgentSettings(BaseModel):
    planner: ModelSelection
    worker: ModelSelection
    reviewer: ModelSelection


class SwarmPlanningSettings(BaseModel):
    human_review_mode: HumanReviewMode = HumanReviewMode.NONE
    plan_review_timeout_hours: int = 24


class SwarmSandboxSettings(BaseModel):
    cpu: str = "4"
    memory: str = "16Gi"
    idle_timeout_in_seconds: int | None = None
    keep_failed_sandboxes: bool = False
    sandbox_disk_id: str | None = None

    @field_validator(
        "sandbox_disk_id",
        mode="before",
    )
    @classmethod
    def normalize_selector_text(cls, value: Any) -> str | None:
        return normalize_optional_text(value)

    @model_validator(mode="after")
    def validate_selectors(self) -> "SwarmSandboxSettings":
        validate_sandbox_selector_values(
            sandbox_disk_id=self.sandbox_disk_id,
            layer_name="sandbox settings",
        )
        return self

    def create_sandbox_selector_kwargs(self) -> dict[str, str]:
        if self.sandbox_disk_id is None:
            raise ValueError("A private sandbox DiskId is required for ACA sandbox execution.")
        return {"disk_id": self.sandbox_disk_id}


class SwarmOptions(BaseModel):
    max_fix_chain_depth: int = 3
    max_replans: int = 1
    planning: SwarmPlanningSettings = Field(default_factory=SwarmPlanningSettings)
    models: SwarmAgentSettings
    copilot_runtime: CopilotRuntimeSettings = Field(default_factory=CopilotRuntimeSettings)
    sandbox: SwarmSandboxSettings = Field(default_factory=SwarmSandboxSettings)


class RunOwner(BaseModel):
    session_id: str

    @property
    def user_id(self) -> str:
        return self.session_id

    @property
    def login(self) -> str:
        return f"session-{self.session_id[:8]}"


class SwarmTaskStatusTransition(BaseModel):
    status: str
    timestamp: datetime = Field(default_factory=utcnow)


class ValidationCommandResult(BaseModel):
    command: str
    exit_code: int
    status: str
    stdout: str | None = None
    stderr: str | None = None


class SwarmTaskState(BaseModel):
    id: str
    title: str
    status: str
    summary: str | None = None
    assignee: str | None = None
    branch_name: str | None = None
    round_number: int | None = None
    dependencies: list[str] = Field(default_factory=list)
    history: list[SwarmTaskStatusTransition] = Field(default_factory=list)
    failure_details: str | None = None
    active_sandbox_id: str | None = None
    target_files: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)
    head_commit_sha: str | None = None
    parent_commit_sha: str | None = None
    changed_files: list[str] = Field(default_factory=list)
    validation_summary: str | None = None
    validation_results: list[ValidationCommandResult] = Field(default_factory=list)

    @property
    def is_completed(self) -> bool:
        return self.status.lower() in {"completed", "succeeded", "failed", "cancelled", "skipped"}


class SwarmPlanState(BaseModel):
    design_document: str | None = None
    tasks: list[SwarmTaskState] = Field(default_factory=list)


class SwarmReviewFinding(BaseModel):
    task_id: str
    severity: str
    description: str


class SwarmReviewFixTask(BaseModel):
    id: str
    title: str
    description: str
    dependencies: list[str] = Field(default_factory=list)
    round_number: int | None = None
    branch_name: str | None = None
    target_files: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)


class SwarmActivitySummary(BaseModel):
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
    findings: list[SwarmReviewFinding] = Field(default_factory=list)
    fix_tasks: list[SwarmReviewFixTask] = Field(default_factory=list)
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
    validation_results: list[ValidationCommandResult] = Field(default_factory=list)


class SwarmMergeResolverSandbox(BaseModel):
    branch_name: str
    round_number: int
    sandbox_id: str


class SwarmBranchHeadState(BaseModel):
    branch_name: str | None = None
    checkpoint_sequence: int | None = None
    commit_sha: str | None = None
    reference_type: str = "Unavailable"


class SwarmMergeState(BaseModel):
    status: str = "Queued"
    resolution_state: str = "NotRequired"
    active_resolution_sandbox_id: str | None = None
    has_unreviewed_changes: bool = False
    has_unapproved_changes: bool = False


class SwarmBranchState(BaseModel):
    branch_name: str | None = None
    current_head_sha: str | None = None
    current_head_checkpoint_sequence: int | None = None
    reviewed_head_sha: str | None = None
    reviewed_checkpoint_sequence: int | None = None
    approved_branch_name: str | None = None
    approved_head_sha: str | None = None
    approved_checkpoint_sequence: int | None = None
    active_wave: int = 1
    current_wave_round: int = 0
    merge_status: str = "Queued"
    merge_resolution_state: str = "NotRequired"
    merge_resolution_sandbox_id: str | None = None

    @model_validator(mode="after")
    def clear_unbacked_approved_branch(self) -> "SwarmBranchState":
        if self.approved_checkpoint_sequence is None and not self.approved_head_sha:
            self.approved_branch_name = None
        return self

    @computed_field(return_type=SwarmBranchHeadState)
    @property
    def current_head(self) -> SwarmBranchHeadState:
        return _build_branch_head_state(
            branch_name=self.branch_name,
            checkpoint_sequence=self.current_head_checkpoint_sequence,
            commit_sha=self.current_head_sha,
        )

    @computed_field(return_type=SwarmBranchHeadState)
    @property
    def reviewed_head(self) -> SwarmBranchHeadState:
        return _build_branch_head_state(
            branch_name=self.branch_name,
            checkpoint_sequence=self.reviewed_checkpoint_sequence,
            commit_sha=self.reviewed_head_sha,
        )

    @computed_field(return_type=SwarmBranchHeadState)
    @property
    def approved_head(self) -> SwarmBranchHeadState:
        return _build_branch_head_state(
            branch_name=self.approved_branch_name,
            checkpoint_sequence=self.approved_checkpoint_sequence,
            commit_sha=self.approved_head_sha,
            allow_branch_reference=False,
        )

    @computed_field(return_type=SwarmMergeState)
    @property
    def merge_state(self) -> SwarmMergeState:
        return SwarmMergeState(
            status=self.merge_status,
            resolution_state=self.merge_resolution_state,
            active_resolution_sandbox_id=self.merge_resolution_sandbox_id,
            has_unreviewed_changes=_head_is_ahead(
                candidate_checkpoint=self.current_head_checkpoint_sequence,
                candidate_sha=self.current_head_sha,
                baseline_checkpoint=self.reviewed_checkpoint_sequence,
                baseline_sha=self.reviewed_head_sha,
            ),
            has_unapproved_changes=_head_is_ahead(
                candidate_checkpoint=self.current_head_checkpoint_sequence,
                candidate_sha=self.current_head_sha,
                baseline_checkpoint=self.approved_checkpoint_sequence,
                baseline_sha=self.approved_head_sha,
            ),
        )


class PlanTaskRevision(BaseModel):
    id: str
    title: str
    description: str | None = None


class PlanFeedbackSubmission(BaseModel):
    action: PlanFeedbackAction
    comments: str | None = None
    revised_tasks: list[PlanTaskRevision] = Field(default_factory=list)
    submitted_at_utc: datetime = Field(default_factory=utcnow)


class RunIntentFlags(BaseModel):
    cancel_requested: bool = False
    suspend_requested: bool = False
    resume_requested: bool = False
    rerun_requested: bool = False
    purge_requested: bool = False


class RunLease(BaseModel):
    holder_id: str
    acquired_at_utc: datetime
    heartbeat_at_utc: datetime
    expires_at_utc: datetime
    lease_token: str | None = None


class RunExecutionState(BaseModel):
    owner_id: str | None = None
    last_command_type: str | None = None
    acquired_at_utc: datetime | None = None
    heartbeat_at_utc: datetime | None = None
    last_progress_at_utc: datetime | None = None
    attempt_count: int = 0


class CoordinatorCheckpoint(BaseModel):
    run_id: str
    phase: str
    status: str
    sequence: int = 0
    updated_at_utc: datetime = Field(default_factory=utcnow)


class CoordinatorCommand(BaseModel):
    run_id: str
    command_type: str
    requested_by: RunOwner
    reason: str | None = None
    requested_at_utc: datetime = Field(default_factory=utcnow)


class RunProjectionSnapshot(BaseModel):
    run_id: str
    owner_user_id: str
    sequence: int
    checksum: str
    summary: dict[str, Any]
    plan: dict[str, Any]
    tasks: dict[str, Any]
    details: dict[str, Any]
    is_terminal: bool
    updated_at_utc: datetime = Field(default_factory=utcnow)


class SwarmRunState(BaseModel):
    id: str
    owner: RunOwner
    title: str | None = None
    prompt: str
    repository_url: str
    base_branch: str | None = None
    target_branch: str | None = None
    runtime_status: str = "Pending"
    status: str = "Queued"
    phase: str = "Queued"
    message: str | None = None
    failure_message: str | None = None
    created_at_utc: datetime = Field(default_factory=utcnow)
    last_updated_at_utc: datetime = Field(default_factory=utcnow)
    options: SwarmOptions
    awaiting_plan_review: bool = False
    plan: SwarmPlanState = Field(default_factory=SwarmPlanState)
    tasks: list[SwarmTaskState] = Field(default_factory=list)
    branch_state: SwarmBranchState = Field(default_factory=SwarmBranchState)
    merge_resolver_sandboxes: list[SwarmMergeResolverSandbox] = Field(default_factory=list)
    planner_summaries: list[SwarmActivitySummary] = Field(default_factory=list)
    worker_summaries: list[SwarmActivitySummary] = Field(default_factory=list)
    reviewer_summaries: list[SwarmActivitySummary] = Field(default_factory=list)
    plan_feedback_history: list[PlanFeedbackSubmission] = Field(default_factory=list)
    total_execution_rounds: int = 0
    consecutive_fix_rounds: int = 0
    replan_count: int = 0
    pending_replan_summary: str | None = None
    pending_replan_findings: list[str] = Field(default_factory=list)
    active_planner_sandbox_id: str | None = None
    active_reviewer_sandbox_id: str | None = None
    publish_status: str | None = None
    publish_error: str | None = None
    pull_request_url: str | None = None
    pull_request_number: int | None = None
    intent: RunIntentFlags = Field(default_factory=RunIntentFlags)
    lease: RunLease | None = None
    execution: RunExecutionState = Field(default_factory=RunExecutionState)
    checkpoint: CoordinatorCheckpoint | None = None

    @property
    def task_count(self) -> int:
        return len(self.tasks)

    @property
    def completed_task_count(self) -> int:
        return sum(1 for task in self.tasks if task.is_completed)

    @property
    def can_cancel(self) -> bool:
        return not self.is_terminal and self.runtime_status != "Suspended"

    @property
    def can_suspend(self) -> bool:
        return not self.is_terminal and self.runtime_status in {"Pending", "Running"}

    @property
    def can_resume(self) -> bool:
        return self.runtime_status == "Suspended"

    @property
    def can_purge(self) -> bool:
        return self.is_terminal

    @property
    def can_rerun(self) -> bool:
        return self.is_terminal

    @property
    def is_terminal(self) -> bool:
        return self.runtime_status in {"Completed", "Failed", "Terminated"}


def _build_branch_head_state(
    *,
    branch_name: str | None,
    checkpoint_sequence: int | None,
    commit_sha: str | None,
    allow_branch_reference: bool = True,
) -> SwarmBranchHeadState:
    if commit_sha:
        reference_type = "Commit"
    elif checkpoint_sequence is not None:
        reference_type = "Checkpoint"
    elif allow_branch_reference and branch_name:
        reference_type = "Branch"
    else:
        reference_type = "Unavailable"
        branch_name = None
    return SwarmBranchHeadState(
        branch_name=branch_name,
        checkpoint_sequence=checkpoint_sequence,
        commit_sha=commit_sha,
        reference_type=reference_type,
    )


def _head_is_ahead(
    *,
    candidate_checkpoint: int | None,
    candidate_sha: str | None,
    baseline_checkpoint: int | None,
    baseline_sha: str | None,
) -> bool:
    if candidate_checkpoint is not None:
        if baseline_checkpoint is None:
            return True
        if candidate_checkpoint > baseline_checkpoint:
            return True
        if candidate_checkpoint < baseline_checkpoint:
            return False
    if candidate_sha:
        return baseline_sha != candidate_sha
    return False
