from __future__ import annotations

from typing import Protocol

import httpx
from pydantic import BaseModel, ConfigDict

from agent_swarm_service.auth.session_store import RunSecretStore
from agent_swarm_service.config import GitHubPublishBackend, ServiceSettings
from agent_swarm_service.orchestration.models import SwarmRunState
from agent_swarm_service.orchestration.sandbox_execution import RepositoryContext, build_integration_branch_name

_GITHUB_API_VERSION = "2022-11-28"


class GitHubPublishResult(BaseModel):
    model_config = ConfigDict(extra="forbid")

    status: str
    target_branch: str
    pull_request_url: str | None = None
    pull_request_number: int | None = None
    commit_sha: str | None = None
    error_message: str | None = None

    @property
    def is_published(self) -> bool:
        return self.status == "Published"


class GitHubPublisherProtocol(Protocol):
    async def publish_run(
        self,
        run: SwarmRunState,
        *,
        repo: RepositoryContext,
        target_branch: str,
    ) -> GitHubPublishResult: ...


class DelegatedGitHubPublisher:
    def __init__(
        self,
        settings: ServiceSettings,
        run_secret_store: RunSecretStore,
        *,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        self._settings = settings
        self._run_secret_store = run_secret_store
        self._client = client

    async def publish_run(
        self,
        run: SwarmRunState,
        *,
        repo: RepositoryContext,
        target_branch: str,
    ) -> GitHubPublishResult:
        target_branch = _resolve_publish_branch(run, repo, target_branch)
        current_head_sha = _require_publish_head(run, target_branch)
        if not repo.is_github:
            raise RuntimeError(
                f"Automatic publish is only supported for github.com repositories. Host '{repo.host}' is not supported."
            )

        run_secret = await self._run_secret_store.get(run.id)
        if run_secret is None:
            raise RuntimeError(
                "No run-scoped GitHub token was available for this run. "
                "Create or rerun the swarm with a fresh PAT before retrying publication."
            )

        headers = {
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {run_secret.token.get_secret_value()}",
            "X-GitHub-Api-Version": _GITHUB_API_VERSION,
        }
        async with self._client_context(headers) as client:
            branch_ref = await self._request_json(
                client,
                "GET",
                f"/repos/{repo.owner}/{repo.name}/git/ref/heads/{target_branch}",
            )
            remote_head_sha = str(branch_ref["object"]["sha"])
            if remote_head_sha != current_head_sha:
                raise RuntimeError(
                    f"Run branch '{target_branch}' head '{remote_head_sha}' does not match expected DTS head '{current_head_sha}'."
                )
            pull_request = await self._ensure_pull_request(client, repo, run, target_branch)
            return GitHubPublishResult(
                status="Published",
                target_branch=target_branch,
                pull_request_url=str(pull_request["html_url"]),
                pull_request_number=int(pull_request["number"]),
                commit_sha=current_head_sha,
            )

    async def _ensure_pull_request(
        self,
        client: httpx.AsyncClient,
        repo: RepositoryContext,
        run: SwarmRunState,
        target_branch: str,
    ) -> dict:
        payload = {
            "title": _build_pull_request_title(run),
            "body": _build_pull_request_body(run, repo, target_branch),
            "head": target_branch,
            "base": repo.base_branch,
        }
        response = await client.post(
            f"{client.base_url}repos/{repo.owner}/{repo.name}/pulls",
            json=payload,
        )
        if response.status_code == 201:
            return response.json()
        if response.status_code == 422:
            existing = await self._request_json(
                client,
                "GET",
                f"/repos/{repo.owner}/{repo.name}/pulls",
                params={"state": "open", "head": f"{repo.owner}:{target_branch}", "base": repo.base_branch},
            )
            if existing:
                pull_request = existing[0]
                return await self._request_json(
                    client,
                    "PATCH",
                    f"/repos/{repo.owner}/{repo.name}/pulls/{pull_request['number']}",
                    json={"title": payload["title"], "body": payload["body"]},
                )
        self._raise_for_github_error(response)

    async def _request_json(
        self,
        client: httpx.AsyncClient,
        method: str,
        path: str,
        *,
        json: dict | None = None,
        params: dict | None = None,
        expected_statuses: set[int] | None = None,
    ) -> dict | list:
        response = await client.request(method, f"{client.base_url}{path.lstrip('/')}", json=json, params=params)
        if response.status_code not in (expected_statuses or {200}):
            self._raise_for_github_error(response)
        return response.json()

    def _raise_for_github_error(self, response: httpx.Response) -> None:
        message = None
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            message = payload.get("message")
        if not message:
            message = response.text or f"GitHub request failed with status {response.status_code}."
        raise RuntimeError(message)

    def _client_context(self, headers: dict[str, str]):
        if self._client is not None:
            return _ExistingClientContext(self._client, headers)
        return httpx.AsyncClient(base_url="https://api.github.com/", headers=headers, timeout=30.0)


class _ExistingClientContext:
    def __init__(self, client: httpx.AsyncClient, headers: dict[str, str]) -> None:
        self._client = client
        self._headers = headers
        self._original_headers: dict[str, str] = {}

    async def __aenter__(self) -> httpx.AsyncClient:
        for key, value in self._headers.items():
            if key in self._client.headers:
                self._original_headers[key] = self._client.headers[key]
            self._client.headers[key] = value
        return self._client

    async def __aexit__(self, exc_type, exc, tb) -> None:
        for key in self._headers:
            if key in self._original_headers:
                self._client.headers[key] = self._original_headers[key]
            else:
                self._client.headers.pop(key, None)


def create_github_publisher(
    settings: ServiceSettings,
    run_secret_store: RunSecretStore,
) -> GitHubPublisherProtocol:
    backend = settings.runtime.github_publish_backend
    if backend not in {GitHubPublishBackend.AUTO, GitHubPublishBackend.GITHUB_API}:
        raise RuntimeError(f"Unsupported GitHub publish backend '{backend.value}'.")
    return DelegatedGitHubPublisher(settings, run_secret_store)


def _require_publish_head(run: SwarmRunState, target_branch: str) -> str:
    if not target_branch.strip():
        raise RuntimeError("Publish requires an established run integration branch.")
    current_head_sha = (run.branch_state.current_head_sha or "").strip()
    if not current_head_sha:
        raise RuntimeError("Publish requires worker-produced commit metadata before a PR can be created or updated.")
    return current_head_sha


def _resolve_publish_branch(
    run: SwarmRunState,
    repo: RepositoryContext,
    target_branch: str,
) -> str:
    requested_branch = target_branch.strip()
    run_target_branch = (run.target_branch or "").strip()
    tracked_branch = (run.branch_state.branch_name or "").strip()
    integration_branch = run_target_branch or tracked_branch or build_integration_branch_name(run, repo)
    if tracked_branch and tracked_branch != integration_branch:
        raise RuntimeError(
            f"Publish must remain pinned to the run integration branch '{integration_branch}', "
            f"but branch state is tracking '{tracked_branch}'."
        )
    if requested_branch and requested_branch != integration_branch:
        raise RuntimeError(
            f"Publish must use the run integration branch '{integration_branch}', not '{requested_branch}'."
        )

    completed_task_branches = _completed_task_branches(run)
    if (
        len(completed_task_branches) > 1
        and integration_branch in completed_task_branches
        and not _is_integration_branch(integration_branch)
    ):
        raise RuntimeError(
            f"Publish must stay pinned to the integrated run branch '{integration_branch}' after fan-in; "
            "refusing to publish a worker task branch."
        )
    return integration_branch


def _build_pull_request_title(run: SwarmRunState) -> str:
    title = run.title or run.prompt
    prefix = "Agent Swarm: "
    return f"{prefix}{title[:80 - len(prefix)]}".rstrip()


def _build_pull_request_body(
    run: SwarmRunState,
    repo: RepositoryContext,
    target_branch: str,
) -> str:
    current_head = run.branch_state.current_head
    reviewed_head = run.branch_state.reviewed_head
    approved_head = run.branch_state.approved_head
    merge_state = run.branch_state.merge_state
    lines = [
        "## Agent Swarm publication",
        "",
        f"- Run ID: `{run.id}`",
        f"- Repository: `{repo.full_name}`",
        f"- Base branch: `{repo.base_branch}`",
        f"- Integration branch: `{target_branch}`",
        f"- Current head: {_format_branch_head(current_head)}",
        f"- Reviewed head: {_format_branch_head(reviewed_head)}",
        f"- Approved head: {_format_branch_head(approved_head)}",
        f"- Merge state: `{merge_state.status}` ({merge_state.resolution_state})",
    ]
    completed_tasks = [task for task in run.tasks if task.is_completed]
    if completed_tasks:
        lines.extend(
            [
                "",
                "### Worker task branches included in this integration head",
                "",
            ]
        )
        for task in completed_tasks:
            validation = f" — {task.validation_summary}" if task.validation_summary else ""
            commit = f" @ `{task.head_commit_sha[:12]}`" if task.head_commit_sha else ""
            branch = f" from `{task.branch_name}`" if task.branch_name else ""
            lines.append(f"- `{task.id}` — {task.title}{branch}{commit}{validation}")
            if task.changed_files:
                lines.extend([f"  - `{path}`" for path in task.changed_files])
    if run.prompt:
        lines.extend(["", "### Prompt", "", run.prompt])
    return "\n".join(lines)


def _format_branch_head(head) -> str:
    if head.commit_sha:
        return f"`{head.branch_name}` @ `{head.commit_sha[:12]}` ({head.reference_type.lower()})"
    if head.checkpoint_sequence is not None:
        return f"`{head.branch_name}` at checkpoint `{head.checkpoint_sequence}` ({head.reference_type.lower()})"
    if head.branch_name:
        return f"`{head.branch_name}` ({head.reference_type.lower()})"
    return "not established"


def _completed_task_branches(run: SwarmRunState) -> set[str]:
    return {
        branch_name
        for task in run.tasks
        if task.is_completed and (branch_name := (task.branch_name or "").strip())
    }


def _is_integration_branch(branch_name: str) -> bool:
    return branch_name.endswith("/integration")
