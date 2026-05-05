from __future__ import annotations

import asyncio
import json
import unittest
from contextlib import contextmanager

from contract_support import create_test_client, ensure_src_on_path, get_openapi_document, to_json_text

ensure_src_on_path()

from agent_swarm_service.app import create_app
from agent_swarm_service.config import ServiceSettings
from agent_swarm_service.dependencies import get_swarm_run_service
from agent_swarm_service.orchestration.dts import DtsSwarmCoordinator, DurableRunOwnershipStore
from agent_swarm_service.orchestration.models import (
    ModelSelection,
    RunOwner,
    SwarmActivitySummary,
    SwarmAgentSettings,
    SwarmBranchState,
    SwarmOptions,
    SwarmPlanState,
    SwarmReviewFixTask,
    SwarmRunState,
    SwarmTaskState,
)
from agent_swarm_service.orchestration.projections import build_details_response, build_event_snapshot
from agent_swarm_service.runtime.storage import InMemoryRuntimeStorageBackend


@contextmanager
def _session_client(*, swarm_run_service):
    try:
        from fastapi.testclient import TestClient
    except ModuleNotFoundError as exc:  # pragma: no cover
        raise unittest.SkipTest("FastAPI test client is unavailable.") from exc

    app = create_app(ServiceSettings.for_local_development())
    app.dependency_overrides[get_swarm_run_service] = lambda: swarm_run_service
    with TestClient(app) as client:
        yield client
    app.dependency_overrides.clear()


def _options() -> SwarmOptions:
    return SwarmOptions(
        models=SwarmAgentSettings(
            planner=ModelSelection(model="claude-opus-4.6"),
            worker=ModelSelection(model="gpt-5.3-codex"),
            reviewer=ModelSelection(model="claude-opus-4.6"),
        )
    )


def _owner() -> RunOwner:
    return RunOwner(session_id="session-101")


def _run(awaitable):
    return asyncio.run(awaitable)


def _make_contract_run(
    run_id: str,
    *,
    runtime_status: str,
    status: str,
    phase: str,
    publish_status: str | None,
    current_head_sha: str | None,
    reviewed_head_sha: str | None,
    approved_head_sha: str | None,
) -> SwarmRunState:
    branch_name = f"swarm/octo/{run_id}/integration"
    task = SwarmTaskState(
        id="task-1",
        title="Validate public branch-state contract",
        status="Completed",
        summary="Expose checkpoint and wave state coherently through the API.",
        branch_name=branch_name,
        round_number=2,
    )
    return SwarmRunState(
        id=run_id,
        owner=_owner(),
        title="Validate branch state contract",
        prompt="Keep the public contract honest while the orchestration model evolves.",
        repository_url="https://github.com/octo/repo",
        base_branch="main",
        target_branch=branch_name,
        runtime_status=runtime_status,
        status=status,
        phase=phase,
        options=_options(),
        plan=SwarmPlanState(
            design_document="Project run-head checkpoint truth through details and events.",
            tasks=[task],
        ),
        tasks=[task],
        branch_state=SwarmBranchState(
            branch_name=branch_name,
            current_head_sha=current_head_sha,
            current_head_checkpoint_sequence=8,
            reviewed_head_sha=reviewed_head_sha,
            reviewed_checkpoint_sequence=6,
            approved_branch_name=branch_name if approved_head_sha is not None else None,
            approved_head_sha=approved_head_sha,
            approved_checkpoint_sequence=6 if approved_head_sha is not None else None,
            active_wave=3,
            current_wave_round=1,
        ),
        reviewer_summaries=[
            SwarmActivitySummary(
                id="reviewer-2",
                kind="reviewer",
                title="Review execution round 2",
                status="completed",
                summary="Reviewer preserved same-wave fix and replan history in the public projection.",
                details="Checkpoint-driven run-head truth remains visible before publish.",
                branch_name=branch_name,
                round_number=2,
                fix_tasks=[
                    SwarmReviewFixTask(
                        id="task-1-fix",
                        title="Address reviewer gap",
                        description="Keep iterating on the same integration branch.",
                        dependencies=["task-1"],
                        round_number=2,
                        branch_name=branch_name,
                    )
                ],
                replan_summary="Replan the next wave from the reviewed head.",
                replan_findings=["Carry the reviewed checkpoint into the next planning wave."],
                publish_status=publish_status,
                pull_request_number=1234 if publish_status is not None else None,
            )
        ],
        publish_status=publish_status,
        pull_request_number=1234 if publish_status is not None else None,
    )


def _make_queued_dts_run() -> SwarmRunState:
    backend = InMemoryRuntimeStorageBackend()
    coordinator = DtsSwarmCoordinator(_NoOpDtsClient(), DurableRunOwnershipStore(backend))
    return _run(
        coordinator.create_run(
            owner=_owner(),
            prompt="Keep approved head hidden until a reviewer actually approves.",
            repository_url="https://github.com/octo/repo",
            base_branch="main",
            options=_options(),
        )
    )


class _StaticSwarmRunService:
    def __init__(self, *, details=None, snapshot=None) -> None:
        self._details = details
        self._snapshot = snapshot

    async def get_details(self, run_id: str):
        return None if self._details is None or self._details.id != run_id else self._details

    async def get_event_snapshot(self, run_id: str):
        return None if self._snapshot is None or self._snapshot.summary.id != run_id else self._snapshot


class _NoOpDtsClient:
    def schedule_new_orchestration(self, orchestrator, *, input=None, instance_id=None, tags=None, version=None, start_at=None, reuse_id_policy=None):
        del orchestrator, input, tags, version, start_at, reuse_id_policy
        return instance_id


def _parse_sse_events(body: str) -> list[tuple[str, object]]:
    events: list[tuple[str, object]] = []
    event_type: str | None = None
    payload: object | None = None
    for line in body.splitlines():
        if line.startswith("event: "):
            event_type = line.removeprefix("event: ")
        elif line.startswith("data: "):
            payload = json.loads(line.removeprefix("data: "))
        elif not line.strip() and event_type is not None:
            events.append((event_type, payload))
            event_type = None
            payload = None
    if event_type is not None:
        events.append((event_type, payload))
    return events


class PublicHttpContractTests(unittest.TestCase):
    def test_health_route_returns_healthy_payload(self) -> None:
        with create_test_client() as client:
            for health_path in ("/health", "/api/health"):
                with self.subTest(path=health_path):
                    response = client.get(health_path)
                    self.assertEqual(response.status_code, 200, msg=f"{health_path} returned {response.status_code}")
                    payload = (
                        response.json()
                        if "application/json" in response.headers.get("content-type", "")
                        else response.text
                    )
                    payload_text = to_json_text(payload).lower()
                    self.assertIn("healthy", payload_text)

    def test_openapi_document_exposes_core_routes_and_omits_retired_auth_surface(self) -> None:
        with create_test_client() as client:
            path, document = get_openapi_document(client)

        self.assertTrue(
            path.endswith("openapi.json") or path.startswith("/openapi"),
            msg=f"OpenAPI document was served from an unexpected path: {path!r}",
        )
        self.assertIn("openapi", document)
        paths = document.get("paths", {})
        for required_path in ("/api/health", "/api/swarm-runs"):
            self.assertIn(required_path, paths)
        # `/health` is the K8s-style probe path used by the Container App; it is
        # intentionally excluded from the OpenAPI document so users see one
        # canonical health endpoint at /api/health.
        self.assertNotIn("/health", paths)
        self.assertNotIn("/api/service-boundary", paths)
        self.assertNotIn("/api/me", paths)
        self.assertFalse(any(path.startswith("/auth/github") for path in paths))

    def test_openapi_document_exposes_branch_head_wave_and_reviewer_contract_fields(self) -> None:
        with create_test_client() as client:
            _, document = get_openapi_document(client)

        schemas = document.get("components", {}).get("schemas", {})
        branch_state = schemas["SwarmRunBranchStateResponse"]["properties"]
        reviewer_summary = schemas["SwarmActivitySummaryResponse"]["properties"]
        task_schema = schemas["SwarmTaskResponse"]["properties"]

        for required_field in (
            "currentHeadSha",
            "currentHeadCheckpointSequence",
            "reviewedHeadSha",
            "reviewedCheckpointSequence",
            "approvedHeadSha",
            "approvedCheckpointSequence",
            "activeWave",
            "currentWaveRound",
            "currentHead",
            "reviewedHead",
            "approvedHead",
            "mergeStatus",
            "mergeState",
        ):
            self.assertIn(required_field, branch_state)
        for required_field in (
            "fixTasks",
            "replanSummary",
            "replanFindings",
            "publishStatus",
            "pullRequestNumber",
            "headCommitSha",
            "parentCommitSha",
            "changedFiles",
            "validationSummary",
            "validationResults",
        ):
            self.assertIn(required_field, reviewer_summary)
        for required_field in (
            "headCommitSha",
            "parentCommitSha",
            "changedFiles",
            "validationSummary",
            "validationResults",
        ):
            self.assertIn(required_field, task_schema)

    def test_openapi_document_exposes_execution_derived_worker_result_fields(self) -> None:
        with create_test_client() as client:
            _, document = get_openapi_document(client)

        task_fields = document.get("components", {}).get("schemas", {})["SwarmTaskResponse"]["properties"]
        for required_field in (
            "headCommitSha",
            "parentCommitSha",
            "validationSummary",
            "changedFiles",
        ):
            with self.subTest(field=required_field):
                self.assertIn(required_field, task_fields)
        self.assertNotIn("harvestedFiles", task_fields)

    def test_service_boundary_route_is_removed(self) -> None:
        with create_test_client() as client:
            response = client.get("/api/service-boundary")

        self.assertEqual(response.status_code, 404)

    def test_swarm_run_list_does_not_require_or_issue_session_cookies(self) -> None:
        with create_test_client() as client:
            response = client.get("/api/swarm-runs")

        self.assertEqual(response.status_code, 200)
        self.assertIsInstance(response.json(), list)
        self.assertNotIn("set-cookie", {key.lower() for key in response.headers})

    def test_swarm_run_creation_requires_pat_in_request_contract(self) -> None:
        with create_test_client() as client:
            invalid = client.post(
                "/api/swarm-runs",
                json={
                    "prompt": "Ship the trusted sample",
                    "repositoryUrl": "https://github.com/octo/repo",
                },
            )
            valid = client.post(
                "/api/swarm-runs",
                json={
                    "prompt": "Ship the trusted sample",
                    "repositoryUrl": "https://github.com/octo/repo",
                    "githubPat": "ghp_example1234567890",
                },
            )

        self.assertEqual(invalid.status_code, 422)
        self.assertEqual(valid.status_code, 201)
        self.assertEqual(valid.json()["repositoryUrl"], "https://github.com/octo/repo")
        self.assertNotIn("githubPat", valid.text)
        self.assertNotIn("github_pat", valid.text)

    def test_run_details_surface_checkpoint_wave_and_explicit_head_truth_contract(self) -> None:
        run = _make_contract_run(
            "swarm-details-contract",
            runtime_status="Running",
            status="running",
            phase="Reviewing",
            publish_status=None,
            current_head_sha=None,
            reviewed_head_sha=None,
            approved_head_sha=None,
        )
        service = _StaticSwarmRunService(details=build_details_response(run))

        with _session_client(swarm_run_service=service) as client:
            response = client.get(f"/api/swarm-runs/{run.id}/details")

        self.assertEqual(response.status_code, 200)
        payload = response.json()
        self.assertEqual(payload["branchState"]["currentHeadCheckpointSequence"], 8)
        self.assertEqual(payload["branchState"]["reviewedCheckpointSequence"], 6)
        self.assertEqual(payload["branchState"]["activeWave"], 3)
        self.assertEqual(payload["branchState"]["currentWaveRound"], 1)
        self.assertNotIn("currentHeadSha", payload["branchState"])
        self.assertNotIn("approvedBranchName", payload["branchState"])
        self.assertNotIn("approvedHeadSha", payload["branchState"])
        self.assertEqual(payload["branchState"]["currentHead"]["referenceType"], "Checkpoint")
        self.assertEqual(payload["branchState"]["reviewedHead"]["referenceType"], "Checkpoint")
        self.assertEqual(payload["branchState"]["approvedHead"]["referenceType"], "Unavailable")
        self.assertNotIn("branchName", payload["branchState"]["approvedHead"])
        self.assertEqual(payload["branchState"]["mergeState"]["status"], "Queued")
        self.assertTrue(payload["branchState"]["mergeState"]["hasUnreviewedChanges"])
        self.assertTrue(payload["branchState"]["mergeState"]["hasUnapprovedChanges"])
        self.assertEqual(payload["reviewerSummaries"][-1]["fixTasks"][0]["branchName"], run.target_branch)
        self.assertEqual(
            payload["reviewerSummaries"][-1]["replanFindings"],
            ["Carry the reviewed checkpoint into the next planning wave."],
        )

    def test_run_details_keep_approved_head_unavailable_before_any_reviewer_approval(self) -> None:
        run = _make_queued_dts_run()
        service = _StaticSwarmRunService(details=build_details_response(run))

        with _session_client(swarm_run_service=service) as client:
            response = client.get(f"/api/swarm-runs/{run.id}/details")

        self.assertEqual(response.status_code, 200)
        branch_state = response.json()["branchState"]
        self.assertEqual(branch_state["currentHead"]["referenceType"], "Branch")
        self.assertEqual(branch_state["approvedHead"]["referenceType"], "Unavailable")
        self.assertNotIn("approvedHeadSha", branch_state)
        self.assertNotIn("approvedBranchName", branch_state)

    def test_projection_snapshot_keeps_approved_head_unavailable_before_any_reviewer_approval(self) -> None:
        run = _make_queued_dts_run()
        branch_state = build_event_snapshot(run).details.model_dump(by_alias=True, exclude_none=True, mode="json")["branchState"]
        self.assertEqual(branch_state["currentHead"]["referenceType"], "Branch")
        self.assertEqual(branch_state["approvedHead"]["referenceType"], "Unavailable")
        self.assertNotIn("approvedHeadSha", branch_state)
        self.assertNotIn("approvedBranchName", branch_state)

    def test_events_stream_projected_wave_checkpoint_and_reviewer_contract(self) -> None:
        commit_sha = "a" * 40
        run = _make_contract_run(
            "swarm-events-contract",
            runtime_status="Completed",
            status="completed",
            phase="Completed",
            publish_status="Published",
            current_head_sha=commit_sha,
            reviewed_head_sha=commit_sha,
            approved_head_sha=commit_sha,
        )
        service = _StaticSwarmRunService(snapshot=build_event_snapshot(run))

        with _session_client(swarm_run_service=service) as client:
            response = client.get(f"/api/swarm-runs/{run.id}/events")

        self.assertEqual(response.status_code, 200)
        events = _parse_sse_events(response.text)
        payloads = {event: payload for event, payload in events}
        self.assertEqual(payloads["details"]["branchState"]["currentHeadCheckpointSequence"], 8)
        self.assertEqual(payloads["details"]["branchState"]["reviewedCheckpointSequence"], 6)
        self.assertEqual(payloads["details"]["branchState"]["approvedCheckpointSequence"], 6)
        self.assertEqual(payloads["details"]["branchState"]["currentHeadSha"], commit_sha)
        self.assertEqual(payloads["details"]["branchState"]["reviewedHeadSha"], commit_sha)
        self.assertEqual(payloads["details"]["branchState"]["approvedHeadSha"], commit_sha)
        self.assertEqual(payloads["details"]["branchState"]["activeWave"], 3)
        self.assertEqual(payloads["details"]["branchState"]["currentWaveRound"], 1)
        self.assertEqual(payloads["details"]["branchState"]["currentHead"]["referenceType"], "Commit")
        self.assertEqual(payloads["details"]["branchState"]["approvedHead"]["referenceType"], "Commit")
        self.assertEqual(payloads["details"]["branchState"]["mergeState"]["status"], "Queued")
        self.assertTrue(payloads["details"]["branchState"]["mergeState"]["hasUnreviewedChanges"])
        self.assertTrue(payloads["details"]["branchState"]["mergeState"]["hasUnapprovedChanges"])
        self.assertEqual(payloads["details"]["reviewerSummaries"][-1]["publishStatus"], "Published")
        self.assertEqual(payloads["details"]["reviewerSummaries"][-1]["pullRequestNumber"], 1234)
        self.assertEqual([event for event, _ in events], ["status", "tasks", "plan", "details", "done"])

    def test_retired_profile_route_is_not_served(self) -> None:
        with create_test_client() as client:
            response = client.get("/api/me")

        self.assertEqual(response.status_code, 404)


if __name__ == "__main__":
    unittest.main()
