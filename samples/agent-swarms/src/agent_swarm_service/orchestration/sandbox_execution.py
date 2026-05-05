from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import UTC, datetime, timedelta
from typing import Any
from urllib.parse import quote, urlparse

from pydantic import BaseModel, ConfigDict, Field

from agent_swarm_service.auth.session_store import RunSecretStore
from agent_swarm_service.config import ServiceSettings
from agent_swarm_service.orchestration.models import (
    CopilotRuntimeSettings,
    PlanFeedbackSubmission,
    ReviewOutcome,
    SwarmPlanState,
    SwarmReviewFinding,
    SwarmReviewFixTask,
    SwarmRunState,
    SwarmTaskState,
    SwarmTaskStatusTransition,
    ValidationCommandResult,
)
from agent_swarm_service.sandboxes.aca_client import sandbox_id
from agent_swarm_service.sandboxes.logs import redact_text
from agent_swarm_service.sandboxes.workspace import (
    DEFAULT_LOG_MIRROR_PATH,
    DEFAULT_WORKSPACE_ROOT,
    WorkspaceFile,
    WorkspaceSnapshot,
    read_json,
    read_text,
    stage_snapshot,
)

_SWARM_ROOT = f"{DEFAULT_WORKSPACE_ROOT}/.swarm"
_REQUEST_PATH = f"{_SWARM_ROOT}/request.json"
_RESULT_PATH = f"{_SWARM_ROOT}/result.json"
_BAKED_RUNTIME_LABEL_KEY = "runtime-contract"
_BAKED_RUNTIME_LABEL_VALUE = "baked-disk-image"
_BAKED_RUNNER_PATH = "/opt/agent-swarm/run-role.py"
logger = logging.getLogger(__name__)


class SandboxLifecycleError(RuntimeError):
    pass


def _trim_failure_excerpt(value: str, *, max_lines: int = 200, max_chars: int = 12000) -> str:
    excerpt = "\n".join(value.strip().splitlines()[-max_lines:])
    if len(excerpt) > max_chars:
        excerpt = f"...[truncated]...\n{excerpt[-max_chars:]}"
    return excerpt


def _dedupe_non_empty_strings(values: list[str] | None) -> list[str]:
    seen: set[str] = set()
    deduped: list[str] = []
    for raw in values or []:
        candidate = str(raw).strip().replace("\\", "/")
        if not candidate:
            continue
        key = candidate.lower()
        if key in seen:
            continue
        seen.add(key)
        deduped.append(candidate)
    return deduped


def _worker_task_payload(task: SwarmTaskState, *, default_branch_name: str | None) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "id": task.id,
        "title": task.title,
        "summary": task.summary,
        "dependencies": task.dependencies,
        "branch_name": task.branch_name or default_branch_name,
        "round_number": task.round_number,
        "validation_commands": task.validation_commands,
    }
    target_files = _dedupe_non_empty_strings(task.target_files)
    acceptance_criteria = _dedupe_non_empty_strings(task.acceptance_criteria)
    if target_files:
        payload["target_files"] = target_files
    if acceptance_criteria:
        payload["acceptance_criteria"] = acceptance_criteria
    return payload


class PlannedTaskArtifact(BaseModel):
    id: str
    title: str
    summary: str | None = None
    dependencies: list[str] = Field(default_factory=list)
    branch_name: str | None = None
    round_number: int | None = None
    target_files: list[str] = Field(default_factory=list)
    acceptance_criteria: list[str] = Field(default_factory=list)
    validation_commands: list[str] = Field(default_factory=list)

    def to_task_state(self) -> SwarmTaskState:
        return SwarmTaskState(
            id=self.id,
            title=self.title,
            status="Pending",
            summary=self.summary,
            dependencies=self.dependencies,
            branch_name=self.branch_name,
            round_number=self.round_number,
            target_files=self.target_files,
            acceptance_criteria=self.acceptance_criteria,
            validation_commands=self.validation_commands,
            history=[SwarmTaskStatusTransition(status="Pending")],
        )


class PlannerExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sandbox_id: str
    summary: str
    design_document: str
    tasks: list[PlannedTaskArtifact]

    def to_plan(self) -> SwarmPlanState:
        tasks = [item.to_task_state() for item in self.tasks]
        return SwarmPlanState(design_document=self.design_document, tasks=tasks)


class WorkerExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sandbox_id: str
    summary: str
    branch_name: str
    round_number: int | None = None
    details: str | None = None
    head_commit_sha: str
    parent_commit_sha: str
    changed_files: list[str]
    validation_summary: str
    validation_results: list[ValidationCommandResult] = Field(default_factory=list)
    no_changes: bool = False


class MergeBranchInput(BaseModel):
    model_config = ConfigDict(extra="forbid")

    task_id: str | None = None
    branch_name: str
    head_commit_sha: str
    parent_commit_sha: str | None = None
    round_number: int | None = None
    changed_files: list[str] = Field(default_factory=list)
    no_changes: bool = False


class MergeExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sandbox_id: str
    target_branch: str
    head_commit_sha: str
    parent_commit_sha: str
    merged_branch_names: list[str] = Field(default_factory=list)
    deleted_branch_names: list[str] = Field(default_factory=list)
    changed_files: list[str] = Field(default_factory=list)
    blocked: bool = False
    blocked_reason: str | None = None


class ReviewerExecutionResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    sandbox_id: str
    outcome: ReviewOutcome
    summary: str
    details: str
    findings: list[SwarmReviewFinding] = Field(default_factory=list)
    fix_tasks: list[SwarmReviewFixTask] = Field(default_factory=list)
    replan_summary: str | None = None
    replan_findings: list[str] = Field(default_factory=list)
    target_branch: str | None = None
    pull_request_url: str | None = None


class RepositoryContext(BaseModel):
    host: str
    owner: str
    name: str
    base_branch: str

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def repo_slug(self) -> str:
        return self.name.replace("-", "_")

    @property
    def is_github(self) -> bool:
        return self.host.lower() == "github.com"


class SandboxAgentRuntimePayload(BaseModel):
    model: str
    copilot_runtime: CopilotRuntimeSettings


class PlannerSandboxActivityInput(BaseModel):
    sandbox: dict[str, Any]
    run_id: str
    created_at_utc: datetime
    prompt: str
    repository_url: str
    repository: RepositoryContext
    agent: SandboxAgentRuntimePayload
    feedback: PlanFeedbackSubmission | None = None
    pending_replan_summary: str | None = None
    pending_replan_findings: list[str] = Field(default_factory=list)


class MergeSandboxActivityInput(BaseModel):
    run: SwarmRunState
    worker_branches: list[MergeBranchInput] = Field(default_factory=list)


class AcaSandboxLifecycleExecutor:
    def __init__(
        self,
        settings: ServiceSettings,
        sandbox_client: Any,
        run_secret_store: RunSecretStore,
        *,
        run_secret_retry_attempts: int = 20,
        run_secret_retry_delay_seconds: float = 0.1,
        run_secret_retry_window_seconds: float = 5.0,
    ) -> None:
        self._settings = settings
        self._sandbox_client = sandbox_client
        self._run_secret_store = run_secret_store
        if run_secret_retry_attempts < 1:
            raise ValueError("run_secret_retry_attempts must be at least 1.")
        if run_secret_retry_delay_seconds < 0:
            raise ValueError("run_secret_retry_delay_seconds cannot be negative.")
        if run_secret_retry_window_seconds < 0:
            raise ValueError("run_secret_retry_window_seconds cannot be negative.")
        self._run_secret_retry_attempts = run_secret_retry_attempts
        self._run_secret_retry_delay_seconds = run_secret_retry_delay_seconds
        self._run_secret_retry_window = timedelta(seconds=run_secret_retry_window_seconds)

    def build_sandbox_request(self, role: str, run: SwarmRunState, task: SwarmTaskState | None = None) -> dict[str, Any]:
        labels = {
            "run-id": run.id,
            "owner": run.owner.login,
            "role": role,
            _BAKED_RUNTIME_LABEL_KEY: _BAKED_RUNTIME_LABEL_VALUE,
        }
        if task is not None:
            labels["task-id"] = task.id
        request = {
            "sandbox_group": self._settings.azure.sandbox_group_name,
            "resource_group": self._settings.azure.resource_group,
            "cpu": run.options.sandbox.cpu,
            "memory": run.options.sandbox.memory,
            "auto_suspend_seconds": run.options.sandbox.idle_timeout_in_seconds or 300,
            "labels": labels,
            "environment": {},
        }
        request.update(run.options.sandbox.create_sandbox_selector_kwargs())
        return request

    async def _build_execution_environment(
        self,
        *,
        run_id: str,
        created_at_utc: datetime,
        copilot_runtime: CopilotRuntimeSettings,
    ) -> dict[str, str]:
        attempts = 1
        if datetime.now(UTC) - created_at_utc <= self._run_secret_retry_window:
            attempts = self._run_secret_retry_attempts
        for attempt in range(attempts):
            secret = await self._run_secret_store.get(run_id)
            if secret is not None:
                token_name = copilot_runtime.token_environment_variable
                token_value = secret.token.get_secret_value()
                shared_environment = {
                    "GH_TOKEN": token_value,
                    "GITHUB_TOKEN": token_value,
                    "GIT_TERMINAL_PROMPT": secret.environment.get("GIT_TERMINAL_PROMPT", "0"),
                    "GCM_INTERACTIVE": secret.environment.get("GCM_INTERACTIVE", "Never"),
                }
                environment = {
                    **shared_environment,
                    token_name: token_value,
                    "SWARM_COPILOT_RUNTIME": copilot_runtime.provider,
                    "SWARM_COPILOT_AUTH_MODE": "run-scoped-pat",
                    "SWARM_COPILOT_TOKEN_ENV_VAR": token_name,
                    "SWARM_COPILOT_USE_LOGGED_IN_USER": "false",
                }
                if not environment.get(token_name):
                    raise SandboxLifecycleError(
                        f"The run-scoped GitHub PAT is not available under the required Copilot token name '{token_name}'."
                    )
                return environment
            if attempt + 1 < attempts and self._run_secret_retry_delay_seconds > 0:
                await asyncio.sleep(self._run_secret_retry_delay_seconds)
        raise SandboxLifecycleError(
            "The run-scoped GitHub token is unavailable or expired. Create or rerun the swarm with a fresh PAT."
        )

    async def build_execution_environment(self, run: SwarmRunState) -> dict[str, str]:
        return await self._build_execution_environment(
            run_id=run.id,
            created_at_utc=run.created_at_utc,
            copilot_runtime=run.options.copilot_runtime,
        )

    async def execute_planner(
        self,
        run: SwarmRunState | PlannerSandboxActivityInput,
        sandbox: dict[str, Any] | None = None,
    ) -> PlannerExecutionResult:
        if isinstance(run, PlannerSandboxActivityInput):
            payload = run
        else:
            if sandbox is None:
                raise ValueError("Planner execution requires sandbox metadata.")
            payload = build_planner_activity_input(run, sandbox)
        request_payload = {
            "sandboxId": sandbox_id(payload.sandbox),
            "runId": payload.run_id,
            "prompt": payload.prompt,
            "repositoryUrl": payload.repository_url,
            "repository": payload.repository.model_dump(mode="json"),
            "agent": payload.agent.model_dump(mode="json"),
            "feedback": payload.feedback.model_dump(mode="json") if payload.feedback else None,
            "pendingReplanSummary": payload.pending_replan_summary,
            "pendingReplanFindings": payload.pending_replan_findings,
        }
        environment = await self._build_execution_environment(
            run_id=payload.run_id,
            created_at_utc=payload.created_at_utc,
            copilot_runtime=payload.agent.copilot_runtime,
        )
        return PlannerExecutionResult.model_validate(
            await self._run_role("planner", payload.sandbox, request_payload, environment=environment)
        )

    async def execute_worker(
        self,
        run: SwarmRunState,
        task: SwarmTaskState,
        sandbox: dict[str, Any],
    ) -> WorkerExecutionResult:
        repo = parse_repository_context(run.repository_url, run.base_branch)
        request_payload = {
            "sandboxId": sandbox_id(sandbox),
            "runId": run.id,
            "prompt": run.prompt,
            "repositoryUrl": run.repository_url,
            "targetBranch": task.branch_name or run.target_branch or build_integration_branch_name(run, repo),
            "branchState": run.branch_state.model_dump(mode="json"),
            "repository": repo.model_dump(mode="json"),
            "agent": _agent_runtime_payload(run, "worker"),
            "task": _worker_task_payload(task, default_branch_name=task.branch_name or run.target_branch),
        }
        environment = await self.build_execution_environment(run)
        result = WorkerExecutionResult.model_validate(
            await self._run_role("worker", sandbox, request_payload, environment=environment)
        )
        if not result.head_commit_sha.strip() or not result.parent_commit_sha.strip():
            raise SandboxLifecycleError("Worker sandbox did not return real commit metadata.")
        if result.no_changes:
            if result.head_commit_sha != result.parent_commit_sha:
                raise SandboxLifecycleError(
                    "Worker sandbox declared no changes but returned different parent/head commit metadata."
                )
            if result.changed_files:
                raise SandboxLifecycleError(
                    "Worker sandbox declared no changes but still returned changed files."
                )
        elif not result.changed_files:
            raise SandboxLifecycleError("Worker sandbox did not return a real changed-file manifest.")
        return result

    async def execute_reviewer(self, run: SwarmRunState, sandbox: dict[str, Any]) -> ReviewerExecutionResult:
        repo = parse_repository_context(run.repository_url, run.base_branch)
        target_branch = run.target_branch or build_integration_branch_name(run, repo)
        if not run.branch_state.current_head_sha:
            raise SandboxLifecycleError(
                "Reviewer sandbox requires a real integration-branch head commit SHA from GitMergeActivity."
            )
        branch_state = run.branch_state.model_copy(update={"branch_name": target_branch})
        request_payload = {
            "sandboxId": sandbox_id(sandbox),
            "runId": run.id,
            "prompt": run.prompt,
            "repositoryUrl": run.repository_url,
            "targetBranch": target_branch,
            "branchState": branch_state.model_dump(mode="json"),
            "pullRequestUrl": run.pull_request_url or build_pull_request_url(repo, target_branch),
            "completedTasks": [
                {
                    "id": task.id,
                    "title": task.title,
                    "summary": task.summary,
                    "branch_name": task.branch_name,
                    "round_number": task.round_number,
                    "head_commit_sha": task.head_commit_sha,
                    "parent_commit_sha": task.parent_commit_sha,
                    "changed_files": task.changed_files,
                    "validation_summary": task.validation_summary,
                    "validation_results": [result.model_dump(mode="json") for result in task.validation_results],
                }
                for task in run.tasks
                if task.status == "InReview"
            ],
            "repository": repo.model_dump(mode="json"),
            "agent": _agent_runtime_payload(run, "reviewer"),
        }
        environment = await self.build_execution_environment(run)
        return ReviewerExecutionResult.model_validate(
            await self._run_role("reviewer", sandbox, request_payload, environment=environment)
        )

    async def execute_merge(
        self,
        run: SwarmRunState,
        worker_branches: list[MergeBranchInput],
        sandbox: dict[str, Any],
    ) -> MergeExecutionResult:
        repo = parse_repository_context(run.repository_url, run.base_branch)
        target_branch = run.target_branch or build_integration_branch_name(run, repo)
        request_payload = {
            "sandboxId": sandbox_id(sandbox),
            "runId": run.id,
            "prompt": run.prompt,
            "repositoryUrl": run.repository_url,
            "targetBranch": target_branch,
            "branchState": run.branch_state.model_dump(mode="json"),
            "repository": repo.model_dump(mode="json"),
            "agent": _agent_runtime_payload(run, "merge"),
            "workerBranches": [item.model_dump(mode="json") for item in worker_branches],
        }
        environment = await self.build_execution_environment(run)
        result = MergeExecutionResult.model_validate(
            await self._run_role("merge", sandbox, request_payload, environment=environment)
        )
        if result.blocked:
            raise SandboxLifecycleError(result.blocked_reason or "Git merge activity was blocked.")
        if not result.head_commit_sha.strip() or not result.parent_commit_sha.strip():
            raise SandboxLifecycleError("Merge sandbox did not return real commit metadata.")
        return result

    async def cleanup_sandbox(self, sandbox: dict[str, Any], *, failed: bool = False) -> None:
        if failed and self._settings.runtime.keep_failed_sandboxes:
            logger.info("Keeping failed sandbox %s", _sandbox_role_log_context(sandbox=sandbox))
            return
        logger.info("Deleting sandbox %s", _sandbox_role_log_context(sandbox=sandbox))
        await asyncio.to_thread(
            self._sandbox_client.delete_sandbox,
            _sandbox_id_value(sandbox),
            str(sandbox["sandbox_group"]),
            resource_group=sandbox.get("resource_group"),
        )
        logger.info("Deleted sandbox %s", _sandbox_role_log_context(sandbox=sandbox))

    async def _run_role(
        self,
        role: str,
        sandbox: dict[str, Any],
        request_payload: dict[str, object],
        *,
        environment: dict[str, str],
        result_payload: dict[str, object] | None = None,
    ) -> dict[str, object]:
        del result_payload
        sandbox_id_value = _sandbox_id_value(sandbox)
        sandbox_group = str(sandbox["sandbox_group"])
        resource_group = sandbox.get("resource_group")
        task = request_payload.get("task")
        task_id = str(task.get("id")) if isinstance(task, dict) and task.get("id") is not None else None
        log_context = _sandbox_role_log_context(
            sandbox=sandbox,
            role=role,
            run_id=str(request_payload.get("runId") or ""),
            task_id=task_id,
        )
        snapshot = WorkspaceSnapshot(
            files=[
            WorkspaceFile(path=_REQUEST_PATH, content=json.dumps(_compact_payload(request_payload), indent=2, sort_keys=True)),
            WorkspaceFile(path=DEFAULT_LOG_MIRROR_PATH, content=""),
            ]
        )
        stage_started = time.monotonic()
        logger.info("Staging sandbox payload %s", log_context)
        await stage_snapshot(
            self._sandbox_client,
            sandbox_id_value,
            sandbox_group,
            snapshot,
            resource_group=resource_group,
        )
        logger.info(
            "Staged sandbox payload elapsed_seconds=%.2f %s",
            time.monotonic() - stage_started,
            log_context,
        )
        command = (
            f"python {_BAKED_RUNNER_PATH} --role {role} --workspace {DEFAULT_WORKSPACE_ROOT} --swarm-root {_SWARM_ROOT}"
        )
        exec_started = time.monotonic()
        logger.info("Starting sandbox exec command=%s %s", command, log_context)
        try:
            exec_result = await asyncio.to_thread(
                self._sandbox_client.exec,
                sandbox_id_value,
                sandbox_group,
                command,
                working_directory=DEFAULT_WORKSPACE_ROOT,
                resource_group=resource_group,
            )
        except Exception:
            logger.exception(
                "Sandbox exec transport failed elapsed_seconds=%.2f %s",
                time.monotonic() - exec_started,
                log_context,
            )
            raise
        exit_code = int(exec_result.exit_code)
        logger.info(
            "Sandbox exec finished exit_code=%s elapsed_seconds=%.2f %s",
            exit_code,
            time.monotonic() - exec_started,
            log_context,
        )
        standard_output = redact_text(str(exec_result.stdout), extra_values=list(environment.values()))
        standard_error = redact_text(str(exec_result.stderr), extra_values=list(environment.values()))
        if exit_code != 0:
            logger.warning("Sandbox exec returned non-zero exit code=%s %s", exit_code, log_context)
            failure_output = standard_error or standard_output
            if not failure_output.strip():
                try:
                    failure_output = _trim_failure_excerpt(
                        redact_text(
                            await read_text(
                                self._sandbox_client,
                                sandbox_id_value,
                                sandbox_group,
                                DEFAULT_LOG_MIRROR_PATH,
                                resource_group=resource_group,
                            ),
                            extra_values=list(environment.values()),
                        )
                    )
                except Exception as exc:
                    logger.warning("Reading mirrored sandbox log failed error=%s %s", exc, log_context)
                    failure_output = f"Mirrored sandbox log could not be read: {exc}"
            message = f"{role.title()} sandbox failed with exit code {exit_code}"
            if failure_output:
                message = f"{message}: {failure_output}"
            raise SandboxLifecycleError(message)
        try:
            logger.info("Reading sandbox result artifact %s", log_context)
            result = await read_json(
                self._sandbox_client,
                sandbox_id_value,
                sandbox_group,
                _RESULT_PATH,
                resource_group=resource_group,
            )
        except Exception as exc:
            logger.exception("Sandbox result artifact read failed %s", log_context)
            raise SandboxLifecycleError(f"{role.title()} sandbox did not produce a valid result artifact.") from exc
        if not isinstance(result, dict):
            raise SandboxLifecycleError(f"{role.title()} sandbox did not produce an object result manifest.")
        logger.info("Loaded sandbox result artifact %s", log_context)
        return result


def planned_task(
    task_id: str,
    title: str,
    summary: str | None,
    *,
    dependencies: list[str] | None = None,
    branch_name: str | None = None,
    round_number: int | None = 1,
    target_files: list[str] | None = None,
    acceptance_criteria: list[str] | None = None,
    validation_commands: list[str] | None = None,
) -> SwarmTaskState:
    return SwarmTaskState(
        id=task_id,
        title=title,
        status="Pending",
        summary=summary,
        branch_name=branch_name,
        round_number=round_number,
        dependencies=list(dependencies or []),
        target_files=list(target_files or []),
        acceptance_criteria=list(acceptance_criteria or []),
        validation_commands=list(validation_commands or []),
        history=[SwarmTaskStatusTransition(status="Pending")],
    )


def parse_repository_context(repository_url: str, base_branch: str | None) -> RepositoryContext:
    parsed = urlparse(repository_url)
    segments = [segment for segment in parsed.path.split("/") if segment]
    owner = segments[0] if segments else "repo-owner"
    name = (segments[1] if len(segments) > 1 else "repository").removesuffix(".git")
    host = (parsed.netloc or "github.com").lower()
    if host == "www.github.com":
        host = "github.com"
    return RepositoryContext(
        host=host,
        owner=owner,
        name=name,
        base_branch=base_branch or "main",
    )


def build_integration_branch_name(run: SwarmRunState, repo: RepositoryContext) -> str:
    return f"swarm/{repo.owner}/{run.id[:8]}/integration"


def build_pull_request_url(repo: RepositoryContext, integration_branch: str | None) -> str | None:
    if not repo.is_github or not integration_branch:
        return None
    base = quote(repo.base_branch, safe="")
    head = quote(integration_branch, safe="")
    return f"https://github.com/{repo.full_name}/compare/{base}...{head}?expand=1"


def _agent_runtime_payload(run: SwarmRunState, role: str) -> dict[str, Any]:
    if run.options.copilot_runtime.provider != "github-copilot-sdk":
        raise SandboxLifecycleError(
            f"Unsupported Copilot runtime provider '{run.options.copilot_runtime.provider}'."
        )
    selected_role = "worker" if role == "merge" else role
    model_selection = getattr(run.options.models, selected_role)
    return {
        "model": model_selection.model,
        "copilot_runtime": run.options.copilot_runtime.model_dump(mode="json"),
    }


def build_planner_activity_input(run: SwarmRunState, sandbox: dict[str, Any]) -> PlannerSandboxActivityInput:
    repo = parse_repository_context(run.repository_url, run.base_branch)
    latest_feedback = run.plan_feedback_history[-1] if run.plan_feedback_history else None
    return PlannerSandboxActivityInput(
        sandbox=sandbox,
        run_id=run.id,
        created_at_utc=run.created_at_utc,
        prompt=run.prompt,
        repository_url=run.repository_url,
        repository=repo,
        agent=SandboxAgentRuntimePayload.model_validate(_agent_runtime_payload(run, "planner")),
        feedback=latest_feedback,
        pending_replan_summary=run.pending_replan_summary,
        pending_replan_findings=run.pending_replan_findings,
    )


def _compact_payload(value: Any) -> Any:
    if isinstance(value, dict):
        return {
            key: _compact_payload(item)
            for key, item in value.items()
            if item is not None
        }
    if isinstance(value, list):
        return [_compact_payload(item) for item in value]
    return value


def _sandbox_role_log_context(
    *,
    sandbox: dict[str, Any],
    role: str | None = None,
    run_id: str | None = None,
    task_id: str | None = None,
) -> str:
    payload: dict[str, Any] = {
        "sandbox_id": _sandbox_id_value(sandbox),
        "sandbox_group": sandbox.get("sandbox_group"),
        "resource_group": sandbox.get("resource_group"),
    }
    if role is not None:
        payload["role"] = role
    if run_id:
        payload["run_id"] = run_id
    if task_id:
        payload["task_id"] = task_id
    return json.dumps(payload, sort_keys=True)


def _sandbox_id_value(sandbox: dict[str, Any]) -> str:
    return sandbox_id(sandbox) or str(sandbox.get("sandbox_id") or "")
