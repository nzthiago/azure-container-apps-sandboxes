from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import shutil
import subprocess
from pathlib import Path
from typing import Any
from urllib.parse import quote, urlparse


class RuntimeContractError(RuntimeError):
    pass


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the Agent Swarm Copilot sandbox contract.")
    parser.add_argument("--role", choices=("planner", "worker", "reviewer", "merge"), required=True)
    parser.add_argument("--request", required=True)
    parser.add_argument("--result", required=True)
    parser.add_argument("--workspace", required=True)
    args = parser.parse_args()

    request = json.loads(Path(args.request).read_text(encoding="utf-8"))
    workspace = Path(args.workspace)
    repo_dir = workspace / "repo"
    token = _require_shared_token(request)
    _require_git()
    _prepare_repository(request, repo_dir, token, role=args.role)

    if args.role == "planner":
        result = _run_planner(request, repo_dir)
    elif args.role == "worker":
        result = _run_worker(request, repo_dir)
    elif args.role == "merge":
        result = _run_merge(request, repo_dir)
    else:
        result = _run_reviewer(request, repo_dir)

    Path(args.result).write_text(json.dumps(result, indent=2, sort_keys=True), encoding="utf-8")
    return 0


def _run_planner(request: dict[str, Any], repo_dir: Path) -> dict[str, Any]:
    prompt = "\n\n".join(
        [
            "Return strict JSON only.",
            "You are the planner for an Agent Swarm backend run.",
            json.dumps(
                {
                    "runId": request["runId"],
                    "prompt": request["prompt"],
                    "repository": request["repository"],
                    "feedback": request.get("feedback"),
                    "pendingReplanSummary": request.get("pendingReplanSummary"),
                    "pendingReplanFindings": request.get("pendingReplanFindings", []),
                    "repoSnapshot": _repo_snapshot(repo_dir),
                },
                indent=2,
                sort_keys=True,
            ),
            """Respond with JSON shaped exactly like:
{
  "summary": "string",
  "design_document": "string",
  "tasks": [
    {
      "id": "task-1",
      "title": "string",
      "summary": "string",
      "dependencies": [],
      "branch_name": "string",
      "round_number": 1,
      "target_files": ["string"],
      "acceptance_criteria": ["string"],
      "validation_commands": ["string"]
    }
  ]
}""",
            (
                "Set target_files and acceptance_criteria explicitly on planner tasks when you want bounded worker "
                "guidance. Do not rely on downstream inference; return empty arrays when no explicit guidance is "
                "needed."
            ),
        ]
    )
    payload = _invoke_copilot(
        request["agent"],
        system_prompt="Plan the run from the real repository snapshot and return JSON only.",
        prompt=prompt,
        working_directory=repo_dir,
    )
    tasks = payload.get("tasks")
    if not isinstance(tasks, list) or not tasks:
        raise RuntimeContractError("Planner runtime did not return any tasks.")
    for item in tasks:
        item.setdefault("round_number", 1)
        item.setdefault("target_files", [])
        item.setdefault("acceptance_criteria", [])
        item.setdefault("validation_commands", [])
    payload["sandbox_id"] = request["sandboxId"]
    return payload


def _run_worker(request: dict[str, Any], repo_dir: Path) -> dict[str, Any]:
    task = request["task"]
    response = _invoke_copilot(
        request["agent"],
        system_prompt=(
            "You are the worker for an Agent Swarm backend run. "
            "Use the repository working tree directly, make any required edits with tools, and return strict JSON only. "
            "If you made repository changes, include a commit_message. "
            "If the task is already satisfied and no repository changes are required, set no_changes to true."
        ),
        prompt="\n\n".join(
            [
                "Return strict JSON only.",
                json.dumps(
                    {
                        "runId": request["runId"],
                        "prompt": request["prompt"],
                        "repository": request["repository"],
                        "branchState": request["branchState"],
                        "task": task,
                        "repoSnapshot": _repo_snapshot(repo_dir),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                """Apply edits directly to the repository working tree before replying.

Respond with JSON shaped exactly like one of:
{
  "summary": "string",
  "details": "string",
  "commit_message": "string",
  "no_changes": false
}

{
  "summary": "string",
  "details": "string",
  "no_changes": true
}""",
            ]
        ),
        working_directory=repo_dir,
        allow_tools=True,
    )
    summary = _require_text_response_field(
        response,
        "summary",
        error_message="Worker runtime did not return a summary.",
    )
    details = _require_text_response_field(
        response,
        "details",
        error_message="Worker runtime did not return details.",
    )
    no_changes = _worker_requested_no_changes(response)

    parent_commit_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    validation_commands = task.get("validation_commands") or []
    validation_results = [_run_validation_command(repo_dir, command) for command in validation_commands]
    validation_summary = _summarize_validation(validation_results)
    worktree_has_changes = bool(_git(repo_dir, "status", "--porcelain").stdout.strip())

    if no_changes:
        if worktree_has_changes:
            raise RuntimeContractError(
                "Worker runtime declared no_changes but left repository modifications in the worktree."
            )
    elif not worktree_has_changes:
        raise RuntimeContractError(
            "Worker runtime did not produce repository changes and did not declare a no-change outcome."
        )

    target_branch = str(request["targetBranch"]).strip()
    if not target_branch:
        raise RuntimeContractError("Worker runtime requires a run target branch.")

    if worktree_has_changes:
        commit_message = _require_text_response_field(
            response,
            "commit_message",
            error_message="Worker runtime did not return a commit message.",
        )
        _git(repo_dir, "add", "-A")
        commit_result = subprocess.run(
            ["git", "commit", "-m", commit_message],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if commit_result.returncode != 0:
            raise RuntimeContractError(commit_result.stderr.strip() or commit_result.stdout.strip() or "git commit failed.")

    _git(repo_dir, "push", "--force", "origin", f"HEAD:refs/heads/{target_branch}")
    head_commit_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    changed_files = _changed_files_between_commits(repo_dir, parent_commit_sha, head_commit_sha)
    if no_changes:
        if changed_files:
            raise RuntimeContractError("Worker runtime declared no_changes but produced changed files.")
    elif not changed_files:
        raise RuntimeContractError("Worker runtime did not produce a real changed-file manifest after commit.")

    return {
        "sandbox_id": request["sandboxId"],
        "summary": summary,
        "details": details,
        "branch_name": target_branch,
        "round_number": task.get("round_number"),
        "head_commit_sha": head_commit_sha,
        "parent_commit_sha": parent_commit_sha,
        "changed_files": changed_files,
        "validation_summary": validation_summary,
        "validation_results": validation_results,
        "no_changes": no_changes,
    }


def _run_reviewer(request: dict[str, Any], repo_dir: Path) -> dict[str, Any]:
    baseline_sha = _resolve_review_baseline(request, repo_dir)
    current_head_sha = str(request["branchState"].get("current_head_sha") or "").strip()
    if not current_head_sha:
        raise RuntimeContractError("Reviewer runtime requires the current run-head commit SHA.")

    response = _invoke_copilot(
        request["agent"],
        system_prompt=(
            "You are the reviewer for an Agent Swarm backend run. "
            "Inspect the real repository diff and return strict JSON only. "
            'The outcome field must be exactly one of "approved", "fixTasks", or "replan" and nothing else.'
        ),
        prompt="\n\n".join(
            [
                "Return strict JSON only.",
                json.dumps(
                    {
                        "runId": request["runId"],
                        "prompt": request["prompt"],
                        "repository": request["repository"],
                        "branchState": request["branchState"],
                        "completedTasks": request.get("completedTasks", []),
                        "reviewBaselineSha": baseline_sha,
                        "currentHeadSha": current_head_sha,
                        "diffStat": _git(repo_dir, "diff", "--stat", baseline_sha, current_head_sha).stdout,
                        "diff": _truncate(
                            _git(repo_dir, "diff", "--unified=0", baseline_sha, current_head_sha).stdout,
                            20000,
                        ),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                """Respond with JSON shaped exactly like:
{
  "outcome": "approved",
  "summary": "string",
  "details": "string",
  "findings": [
    {"task_id": "task-1", "severity": "medium", "description": "string"}
  ],
   "fix_tasks": [
     {
       "id": "fix-task-1",
       "title": "string",
       "description": "string",
       "dependencies": ["task-1"],
       "round_number": 2,
       "branch_name": "string",
       "target_files": ["string"],
       "acceptance_criteria": ["string"]
     }
   ],
   "replan_summary": null,
   "replan_findings": []
}""",
                (
                    "Set target_files and acceptance_criteria explicitly on fix_tasks when you want bounded "
                    "follow-up work. Do not rely on downstream inference; return empty arrays when no explicit "
                    "guidance is needed."
                ),
                (
                    'The "outcome" field must be exactly one of "approved", "fixTasks", or "replan" '
                    "with the same casing shown here. Do not return any other token."
                ),
                """Examples:
1. Approved review
{
  "outcome": "approved",
  "summary": "The change is ready.",
  "details": "No further work is required.",
  "findings": [],
  "fix_tasks": [],
  "replan_summary": null,
  "replan_findings": []
}

2. Same-wave follow-up work
{
  "outcome": "fixTasks",
  "summary": "A small follow-up is required.",
  "details": "Keep the current integration branch and add a fix task.",
  "findings": [{"task_id": "task-1", "severity": "medium", "description": "Add the missing validation."}],
  "fix_tasks": [
     {
       "id": "fix-task-1",
       "title": "Add missing validation",
       "description": "Update the current branch with the missing validation path.",
       "dependencies": ["task-1"],
       "round_number": 2,
       "branch_name": "swarm/example/run/integration",
       "target_files": ["README.md"],
       "acceptance_criteria": ["README.md includes the missing validation guidance."]
     }
   ],
   "replan_summary": null,
  "replan_findings": []
}

3. Return to planning
{
  "outcome": "replan",
  "summary": "The current wave needs replanning.",
  "details": "Do not create fix tasks; send the run back to the planner.",
  "findings": [{"task_id": "task-1", "severity": "high", "description": "The current approach is wrong for the requested goal."}],
  "fix_tasks": [],
  "replan_summary": "Replan the next wave from the reviewer findings.",
  "replan_findings": ["The current solution approach should be replaced."]
}""",
                (
                    'Never return "Approved", "FixTasks", "Replan", "Rejected", or any other synonym. '
                    'Return only "approved", "fixTasks", or "replan".'
                ),
            ]
        ),
        working_directory=repo_dir,
    )
    response["outcome"] = _canonicalize_reviewer_outcome(response.get("outcome"))
    response["sandbox_id"] = request["sandboxId"]
    response["target_branch"] = request.get("targetBranch")
    response["pull_request_url"] = request.get("pullRequestUrl")
    return response


def _run_merge(request: dict[str, Any], repo_dir: Path) -> dict[str, Any]:
    target_branch = str(request.get("targetBranch") or "").strip()
    if not target_branch:
        raise RuntimeContractError("Git merge activity requires a run integration branch.")

    parent_commit_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    merged_branch_names: list[str] = []
    blocked_reason: str | None = None

    for position, worker_branch in enumerate(request.get("workerBranches") or [], start=1):
        branch_name = str(worker_branch.get("branch_name") or "").strip()
        head_commit_sha = str(worker_branch.get("head_commit_sha") or "").strip()
        if not branch_name or not head_commit_sha:
            raise RuntimeContractError("Git merge activity requires non-empty worker branch names and head SHAs.")
        if branch_name == target_branch:
            if _git(repo_dir, "rev-parse", "HEAD").stdout.strip() != head_commit_sha:
                _git(repo_dir, "reset", "--hard", head_commit_sha)
                merged_branch_names.append(branch_name)
            continue
        if _git_change_already_integrated(repo_dir, "HEAD", head_commit_sha):
            continue
        cherry_pick = subprocess.run(
            ["git", "cherry-pick", "--allow-empty", head_commit_sha],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if cherry_pick.returncode == 0:
            merged_branch_names.append(branch_name)
            continue
        conflict_output = cherry_pick.stderr.strip() or cherry_pick.stdout.strip() or "git cherry-pick failed."
        blocked_reason = _resolve_merge_conflict(
            request,
            repo_dir,
            branch_name=branch_name,
            head_commit_sha=head_commit_sha,
            target_branch=target_branch,
            position=position,
            conflict_output=conflict_output,
        )
        if blocked_reason is not None:
            break
        merged_branch_names.append(branch_name)

    if blocked_reason is not None:
        _abort_git_operation_if_active(repo_dir)
        current_head_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
        return {
            "sandbox_id": request["sandboxId"],
            "target_branch": target_branch,
            "head_commit_sha": current_head_sha,
            "parent_commit_sha": parent_commit_sha,
            "merged_branch_names": merged_branch_names,
            "deleted_branch_names": [],
            "changed_files": _changed_files_between_commits(repo_dir, parent_commit_sha, current_head_sha),
            "blocked": True,
            "blocked_reason": blocked_reason,
        }

    _git(repo_dir, "push", "--force", "origin", f"HEAD:refs/heads/{target_branch}")
    head_commit_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    deleted_branch_names = _delete_merged_remote_worker_branches(
        request,
        repo_dir,
        target_branch=target_branch,
        merged_branch_names=merged_branch_names,
    )
    return {
        "sandbox_id": request["sandboxId"],
        "target_branch": target_branch,
        "head_commit_sha": head_commit_sha,
        "parent_commit_sha": parent_commit_sha,
        "merged_branch_names": merged_branch_names,
        "deleted_branch_names": deleted_branch_names,
        "changed_files": _changed_files_between_commits(repo_dir, parent_commit_sha, head_commit_sha),
        "blocked": False,
        "blocked_reason": None,
    }


def _delete_merged_remote_worker_branches(
    request: dict[str, Any],
    repo_dir: Path,
    *,
    target_branch: str,
    merged_branch_names: list[str],
) -> list[str]:
    deleted_branch_names: list[str] = []
    merged_branch_name_set = set(merged_branch_names)
    seen: set[str] = set()
    for worker_branch in request.get("workerBranches") or []:
        branch_name = str(worker_branch.get("branch_name") or "").strip()
        head_commit_sha = str(worker_branch.get("head_commit_sha") or "").strip()
        if (
            not branch_name
            or not head_commit_sha
            or bool(worker_branch.get("no_changes"))
            or branch_name == target_branch
            or branch_name in seen
        ):
            continue
        seen.add(branch_name)
        if branch_name not in merged_branch_name_set and not _git_change_already_integrated(
            repo_dir,
            "HEAD",
            head_commit_sha,
        ):
            continue
        delete_result = subprocess.run(
            ["git", "push", "origin", "--delete", branch_name],
            cwd=repo_dir,
            capture_output=True,
            text=True,
            check=False,
        )
        if delete_result.returncode == 0 or _remote_branch_was_already_absent(delete_result):
            deleted_branch_names.append(branch_name)
            continue
        message = delete_result.stderr.strip() or delete_result.stdout.strip() or "git push --delete failed."
        raise RuntimeContractError(f"Failed to delete merged worker branch '{branch_name}': {message}")
    return deleted_branch_names


def _git_change_already_integrated(repo_dir: Path, integrated_ref: str, candidate_commit_sha: str) -> bool:
    if _git_commit_contains(repo_dir, integrated_ref, candidate_commit_sha):
        return True
    result = subprocess.run(
        ["git", "cherry", integrated_ref, candidate_commit_sha],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        return False
    lines = [line.strip() for line in result.stdout.splitlines() if line.strip()]
    return bool(lines) and all(line.startswith("-") for line in lines)


def _remote_branch_was_already_absent(result: subprocess.CompletedProcess[str]) -> bool:
    output = f"{result.stderr}\n{result.stdout}".lower()
    return "remote ref does not exist" in output or (
        "unable to delete" in output and "not found" in output
    )


def _canonicalize_reviewer_outcome(value: Any) -> str:
    if value == "approved":
        return "Approved"
    if value == "fixTasks":
        return "FixTasks"
    if value == "replan":
        return "Replan"
    raise RuntimeContractError(
        'Reviewer runtime must return outcome exactly one of "approved", "fixTasks", or "replan".'
    )


def _require_shared_token(request: dict[str, Any]) -> str:
    token_env = str(request["agent"]["copilot_runtime"]["token_environment_variable"]).strip() or "GH_TOKEN"
    token = str(os.getenv(token_env, "")).strip()
    if not token:
        raise RuntimeContractError(f"Missing required Copilot token environment variable '{token_env}'.")
    for alias in ("GH_TOKEN", "GITHUB_TOKEN"):
        alias_value = str(os.getenv(alias, "")).strip()
        if not alias_value:
            raise RuntimeContractError(f"Missing required git token environment variable '{alias}'.")
        if alias_value != token:
            raise RuntimeContractError(f"Environment variable '{alias}' does not match '{token_env}'.")
    return token


def _require_git() -> None:
    if shutil.which("git") is None:
        raise RuntimeContractError("The sandbox image does not include git.")


def _prepare_repository(request: dict[str, Any], repo_dir: Path, token: str, *, role: str) -> dict[str, Any]:
    repo_dir.parent.mkdir(parents=True, exist_ok=True)
    repository_url = str(request["repositoryUrl"]).strip()
    authenticated_url = _authenticated_repo_url(repository_url, token)
    base_branch = str(request["repository"]["base_branch"]).strip()
    target_branch = str(request.get("targetBranch") or "").strip()
    branch_state = request.get("branchState") or {}
    current_head_sha = str(branch_state.get("current_head_sha") or "").strip()

    if (repo_dir / ".git").exists():
        _git(repo_dir, "remote", "set-url", "origin", authenticated_url)
        _git(repo_dir, "fetch", "--prune", "origin")
    else:
        repo_dir.parent.mkdir(parents=True, exist_ok=True)
        subprocess.run(["git", "clone", authenticated_url, str(repo_dir)], check=True, capture_output=True, text=True)
    _git(repo_dir, "config", "user.name", "Agent Swarm")
    _git(repo_dir, "config", "user.email", "agent-swarm@users.noreply.github.com")
    _git(repo_dir, "fetch", "origin", base_branch)
    if target_branch:
        subprocess.run(["git", "fetch", "origin", target_branch], cwd=repo_dir, capture_output=True, text=True, check=False)
    if role == "merge":
        for worker_branch in request.get("workerBranches") or []:
            branch_name = str(worker_branch.get("branch_name") or "").strip()
            if branch_name:
                subprocess.run(
                    ["git", "fetch", "origin", branch_name],
                    cwd=repo_dir,
                    capture_output=True,
                    text=True,
                    check=False,
                )

    if role == "planner":
        _git(repo_dir, "checkout", "-B", base_branch, f"origin/{base_branch}")
    elif role == "worker":
        if target_branch and _has_ref(repo_dir, f"refs/remotes/origin/{target_branch}"):
            _git(repo_dir, "checkout", "-B", target_branch, f"origin/{target_branch}")
        else:
            _git(repo_dir, "checkout", "-B", target_branch, f"origin/{base_branch}")
        if current_head_sha:
            _git(repo_dir, "reset", "--hard", current_head_sha)
    elif role == "merge":
        if not target_branch:
            raise RuntimeContractError("Git merge activity requires a run integration branch.")
        if target_branch and _has_ref(repo_dir, f"refs/remotes/origin/{target_branch}"):
            _git(repo_dir, "checkout", "-B", target_branch, f"origin/{target_branch}")
        else:
            _git(repo_dir, "checkout", "-B", target_branch, f"origin/{base_branch}")
        if current_head_sha:
            _git(repo_dir, "reset", "--hard", current_head_sha)
    else:
        if not target_branch:
            raise RuntimeContractError("Reviewer runtime requires a run target branch.")
        if not current_head_sha:
            raise RuntimeContractError("Reviewer runtime requires the current run-head commit SHA.")
        _git(repo_dir, "checkout", "-B", target_branch, current_head_sha)
    return request["repository"]


def _resolve_review_baseline(request: dict[str, Any], repo_dir: Path) -> str:
    branch_state = request["branchState"]
    reviewed_head_sha = str(branch_state.get("reviewed_head_sha") or "").strip()
    approved_head_sha = str(branch_state.get("approved_head_sha") or "").strip()
    current_wave_round = int(branch_state.get("current_wave_round") or 0)
    active_wave = int(branch_state.get("active_wave") or 1)
    if reviewed_head_sha:
        return reviewed_head_sha
    if approved_head_sha:
        return approved_head_sha
    if current_wave_round > 0 or active_wave > 1:
        raise RuntimeContractError("Reviewer runtime could not resolve the required review baseline commit.")
    base_branch = str(request["repository"]["base_branch"]).strip()
    return _git(repo_dir, "rev-parse", f"origin/{base_branch}").stdout.strip()


def _repo_snapshot(repo_dir: Path) -> dict[str, Any]:
    head_sha = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    tracked_files = [
        line.strip()
        for line in _git(repo_dir, "ls-tree", "--full-tree", "-r", "--name-only", "HEAD").stdout.splitlines()[:200]
        if line.strip()
    ]
    return {
        "head_sha": head_sha,
        "tracked_files": tracked_files,
        "status": _git(repo_dir, "status", "--short").stdout.splitlines(),
    }


def _invoke_copilot(
    agent: dict[str, Any], *, system_prompt: str, prompt: str, working_directory: Path, allow_tools: bool = False
) -> dict[str, Any]:
    runtime = agent["copilot_runtime"]
    provider = str(runtime.get("provider") or "").strip()
    if provider != "github-copilot-sdk":
        raise RuntimeContractError(f"Unsupported Copilot runtime provider '{provider}'.")

    try:
        from copilot import CopilotClient
        from copilot.generated.session_events import AssistantMessageData
        from copilot.session import PermissionHandler
    except Exception as exc:  # pragma: no cover - sandbox-only dependency
        raise RuntimeContractError("GitHub Copilot SDK runtime is unavailable in the sandbox image.") from exc

    token_env = str(runtime.get("token_environment_variable") or "GH_TOKEN").strip()
    token = str(os.getenv(token_env, "")).strip()
    if not token:
        raise RuntimeContractError(f"Missing required Copilot token environment variable '{token_env}'.")

    model = str(agent.get("model") or "").strip()
    if not model:
        raise RuntimeContractError("Copilot runtime requires an explicit model.")

    async def _run() -> dict[str, Any]:
        session_kwargs: dict[str, Any] = {
            "on_permission_request": PermissionHandler.approve_all,
            "github_token": token,
            "model": model,
            "system_message": {"mode": "append", "content": system_prompt},
            "working_directory": str(working_directory),
        }
        if not allow_tools:
            session_kwargs["available_tools"] = []

        async with CopilotClient() as client:
            async with await client.create_session(**session_kwargs) as session:
                response_text = await _send_copilot_prompt(
                    session,
                    prompt,
                    assistant_message_type=AssistantMessageData,
                )
                try:
                    return _parse_json_response(response_text)
                except RuntimeContractError as exc:
                    retry_prompt = _build_json_repair_prompt(str(exc))
                    retry_text = await _send_copilot_prompt(
                        session,
                        retry_prompt,
                        assistant_message_type=AssistantMessageData,
                    )
                    try:
                        return _parse_json_response(retry_text)
                    except RuntimeContractError as retry_exc:
                        raise RuntimeContractError(
                            f"{retry_exc} Initial parse error: {exc}. "
                            f"Last response preview: {_response_preview(retry_text)}"
                        ) from retry_exc

    return asyncio.run(_run())


async def _send_copilot_prompt(
    session: Any,
    prompt: str,
    *,
    assistant_message_type: type[Any],
    timeout: float = 300.0,
) -> str:
    response = await session.send_and_wait(prompt, timeout=timeout)
    if response is not None and isinstance(response.data, assistant_message_type):
        return response.data.content
    for event in reversed(await session.get_messages()):
        if isinstance(event.data, assistant_message_type):
            return event.data.content
    raise RuntimeContractError("Copilot SDK response did not include a text completion.")


def _build_json_repair_prompt(error_message: str) -> str:
    return (
        "Your last response did not satisfy the runtime JSON contract "
        f"({error_message}). Reply again with exactly one valid JSON object and nothing else. "
        "Do not use markdown fences or commentary. Preserve the same semantic payload, and if you include multiline "
        "content such as file contents, escape newlines, quotes, and backslashes exactly as required by JSON."
    )


def _parse_json_response(text: str) -> dict[str, Any]:
    stripped = text.strip()
    if not stripped:
        raise RuntimeContractError("Copilot SDK response was empty.")
    try:
        payload = json.loads(stripped)
    except json.JSONDecodeError as exc:
        payload = _extract_single_json_object(stripped)
        if payload is None:
            raise RuntimeContractError(f"Copilot SDK response was not valid JSON: {exc.msg}.") from exc
    if not isinstance(payload, dict):
        raise RuntimeContractError("Copilot SDK response JSON must be an object.")
    return payload


def _extract_single_json_object(text: str) -> dict[str, Any] | None:
    matches: list[dict[str, Any]] = []
    for candidate, start, end in _iter_json_object_candidates(text):
        try:
            payload = json.loads(candidate)
        except json.JSONDecodeError:
            continue
        if isinstance(payload, dict):
            matches.append(
                {
                    "payload": payload,
                    "prefix": text[:start].strip(),
                    "suffix": text[end:].strip(),
                }
            )
    if not matches:
        return None
    clean_matches = [item for item in matches if not item["prefix"] and not item["suffix"]]
    selected = clean_matches or matches
    if len(selected) != 1:
        raise RuntimeContractError("Copilot SDK response did not contain a single JSON object.")
    return selected[0]["payload"]


def _iter_json_object_candidates(text: str):
    start: int | None = None
    depth = 0
    in_string = False
    escape = False
    for index, character in enumerate(text):
        if start is None:
            if character == "{":
                start = index
                depth = 1
                in_string = False
                escape = False
            continue
        if in_string:
            if escape:
                escape = False
            elif character == "\\":
                escape = True
            elif character == '"':
                in_string = False
            continue
        if character == '"':
            in_string = True
        elif character == "{":
            depth += 1
        elif character == "}":
            depth -= 1
            if depth == 0:
                yield text[start : index + 1], start, index + 1
                start = None


def _response_preview(text: str, max_length: int = 500) -> str:
    compact = re.sub(r"\s+", " ", text).strip()
    if not compact:
        return "<empty>"
    return _truncate(compact, max_length)


def _require_text_response_field(response: dict[str, Any], field: str, *, error_message: str) -> str:
    value = response.get(field)
    if not isinstance(value, str) or not value.strip():
        raise RuntimeContractError(error_message)
    return value.strip()


def _worker_requested_no_changes(response: dict[str, Any]) -> bool:
    value = response.get("no_changes", False)
    if isinstance(value, bool):
        return value
    raise RuntimeContractError("Worker runtime field 'no_changes' must be a boolean when provided.")


def _changed_files_between_commits(repo_dir: Path, parent_commit_sha: str, head_commit_sha: str) -> list[str]:
    if parent_commit_sha == head_commit_sha:
        return []
    return [
        line.strip()
        for line in _git(repo_dir, "diff", "--name-only", parent_commit_sha, head_commit_sha).stdout.splitlines()
        if line.strip()
    ]


def _run_validation_command(repo_dir: Path, command: str) -> dict[str, Any]:
    try:
        result = subprocess.run(
            command,
            cwd=repo_dir,
            shell=True,
            capture_output=True,
            text=True,
            check=False,
        )
    except OSError as exc:
        raise RuntimeContractError(f"Validation command '{command}' could not be started.") from exc
    return {
        "command": command,
        "exit_code": int(result.returncode),
        "status": "Succeeded" if result.returncode == 0 else "Failed",
        "stdout": _truncate(result.stdout, 12000),
        "stderr": _truncate(result.stderr, 12000),
    }


def _summarize_validation(results: list[dict[str, Any]]) -> str:
    if not results:
        return "No validation commands were requested."
    succeeded = sum(1 for item in results if item["exit_code"] == 0)
    return f"Ran {len(results)} validation command(s); {succeeded} succeeded and {len(results) - succeeded} failed."


def _authenticated_repo_url(repository_url: str, token: str) -> str:
    parsed = urlparse(repository_url)
    if parsed.scheme != "https":
        raise RuntimeContractError("Only https GitHub repository URLs are supported by the sandbox runtime contract.")
    username = "x-access-token"
    netloc = f"{quote(username, safe='')}:{quote(token, safe='')}@{parsed.netloc}"
    return parsed._replace(netloc=netloc).geturl()


def _has_ref(repo_dir: Path, ref_name: str) -> bool:
    result = subprocess.run(
        ["git", "show-ref", "--verify", "--quiet", ref_name],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _git_commit_contains(repo_dir: Path, ancestor: str, descendant: str) -> bool:
    result = subprocess.run(
        ["git", "merge-base", "--is-ancestor", descendant, ancestor],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    return result.returncode == 0


def _abort_git_operation_if_active(repo_dir: Path) -> None:
    for args in (("cherry-pick", "--abort"), ("merge", "--abort")):
        subprocess.run(["git", *args], cwd=repo_dir, capture_output=True, text=True, check=False)


def _resolve_merge_conflict(
    request: dict[str, Any],
    repo_dir: Path,
    *,
    branch_name: str,
    head_commit_sha: str,
    target_branch: str,
    position: int,
    conflict_output: str,
) -> str | None:
    response = _invoke_copilot(
        request["agent"],
        system_prompt=(
            "You are resolving a git cherry-pick conflict for the Agent Swarm merge activity. "
            "Use repository tools to inspect and edit the real conflicted worktree. "
            "Resolve all conflicts, stage the files, and finish the in-progress cherry-pick. "
            "Return strict JSON only describing whether the conflict was resolved."
        ),
        prompt="\n\n".join(
            [
                "Return strict JSON only.",
                json.dumps(
                    {
                        "runId": request["runId"],
                        "targetBranch": target_branch,
                        "workerBranch": branch_name,
                        "workerHeadSha": head_commit_sha,
                        "sequencePosition": position,
                        "conflictOutput": conflict_output,
                        "repoSnapshot": _repo_snapshot(repo_dir),
                    },
                    indent=2,
                    sort_keys=True,
                ),
                """Respond with JSON shaped exactly like:
{
  "resolved": true,
  "summary": "string",
  "details": "string"
}

or

{
  "resolved": false,
  "summary": "string",
  "details": "string"
}""",
            ]
        ),
        working_directory=repo_dir,
        allow_tools=True,
    )
    resolved = response.get("resolved")
    if not isinstance(resolved, bool):
        raise RuntimeContractError("Merge conflict resolver must return a boolean 'resolved' field.")
    summary = _require_text_response_field(
        response,
        "summary",
        error_message="Merge conflict resolver did not return a summary.",
    )
    details = _require_text_response_field(
        response,
        "details",
        error_message="Merge conflict resolver did not return details.",
    )
    if not resolved:
        return f"{summary} {details}".strip()
    unmerged = _git(repo_dir, "diff", "--name-only", "--diff-filter=U").stdout.strip()
    if unmerged:
        return (
            f"Copilot conflict resolution for '{branch_name}' reported success but left unmerged files: "
            f"{unmerged}. Details: {details}"
        )
    status_output = _git(repo_dir, "status", "--porcelain").stdout
    if "UU " in status_output or "AA " in status_output:
        return (
            f"Copilot conflict resolution for '{branch_name}' left unresolved index entries. "
            f"Details: {details}"
        )
    head_commit_sha_after = _git(repo_dir, "rev-parse", "HEAD").stdout.strip()
    if head_commit_sha_after == head_commit_sha:
        return (
            f"Copilot conflict resolution for '{branch_name}' did not create an integration commit on "
            f"'{target_branch}'. Details: {details}"
        )
    return None


def _git(repo_dir: Path, *args: str) -> subprocess.CompletedProcess[str]:
    result = subprocess.run(
        ["git", *args],
        cwd=repo_dir,
        capture_output=True,
        text=True,
        check=False,
    )
    if result.returncode != 0:
        raise RuntimeContractError(result.stderr.strip() or result.stdout.strip() or f"git {' '.join(args)} failed.")
    return result


def _truncate(value: str, max_length: int) -> str:
    return value if len(value) <= max_length else f"{value[:max_length]}\n...[truncated]..."


if __name__ == "__main__":  # pragma: no cover - sandbox entrypoint
    raise SystemExit(main())
