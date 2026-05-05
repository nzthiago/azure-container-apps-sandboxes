from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from azure.mgmt.sandbox import SandboxGroupManagementClient
from azure.sandbox import SandboxClient

from agent_swarm_service.api.routers import health, swarm_runs
from agent_swarm_service.auth.session_store import DurableRunSecretStore
from agent_swarm_service.config import (
    OrchestrationBackend,
    ServiceSettings,
    load_settings,
)
from agent_swarm_service.github.publishing import create_github_publisher
from agent_swarm_service.orchestration.coordinator import DurableCoordinatorExecutionLoop, DurableSwarmCoordinator
from agent_swarm_service.orchestration.dts import (
    DtsSwarmCoordinator,
    DtsSwarmRuntimeHost,
    DurableRunOwnershipStore,
)
from agent_swarm_service.orchestration.sandbox_execution import AcaSandboxLifecycleExecutor
from agent_swarm_service.runtime.storage import create_runtime_storage
from agent_swarm_service.services.swarm_runs import SwarmRunService


def create_app(
    settings: ServiceSettings | None = None,
    *,
    allow_placeholder_settings: bool = True,
) -> FastAPI:
    resolved_settings = settings or load_settings(allow_default=allow_placeholder_settings)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        runtime_host = getattr(app.state, "orchestration_runtime_host", None)
        worker = getattr(app.state, "background_coordinator_loop", None)
        if runtime_host is not None and resolved_settings.dts.worker_enabled:
            await runtime_host.start()
        elif worker is not None and resolved_settings.storage.background_worker_enabled:
            await worker.start()
        try:
            yield
        finally:
            if runtime_host is not None:
                await runtime_host.stop()
            elif worker is not None:
                await worker.stop()

    app = FastAPI(
        title="Agent Swarm Service",
        version="0.1.0",
        description="Python FastAPI backend for the ACA Sandboxes agent swarm sample.",
        lifespan=lifespan,
        docs_url="/api/docs",
        redoc_url="/api/redoc",
        openapi_url="/api/openapi.json",
    )

    runtime_storage = create_runtime_storage(resolved_settings)
    run_secret_store = DurableRunSecretStore(runtime_storage)
    sandbox_client = SandboxClient(
        resource_group=resolved_settings.azure.resource_group,
        subscription_id=resolved_settings.azure.subscription_id,
    )
    sandbox_lifecycle = AcaSandboxLifecycleExecutor(
        resolved_settings,
        sandbox_client,
        run_secret_store,
    )
    publish_service = create_github_publisher(resolved_settings, run_secret_store)

    runtime_host = None
    background_loop = None
    if resolved_settings.orchestration.backend is OrchestrationBackend.DTS:
        runtime_host = DtsSwarmRuntimeHost(
            resolved_settings,
            sandbox_lifecycle=sandbox_lifecycle,
            sandbox_client=sandbox_client,
            publish_service=publish_service,
        )
        coordinator = DtsSwarmCoordinator(runtime_host.client, DurableRunOwnershipStore(runtime_storage))
    else:
        coordinator = DurableSwarmCoordinator(
            runtime_storage,
            queue_name=resolved_settings.storage.coordinator_queue_name,
            lease_duration_seconds=resolved_settings.storage.lease_duration_seconds,
        )
        background_loop = DurableCoordinatorExecutionLoop(
            coordinator,
            sandbox_lifecycle=sandbox_lifecycle,
            sandbox_client=sandbox_client,
            publish_service=publish_service,
            poll_interval_seconds=resolved_settings.storage.poll_interval_seconds,
            visibility_timeout_seconds=resolved_settings.storage.queue_visibility_timeout_seconds,
        )

    app.state.settings = resolved_settings
    app.state.runtime_storage = runtime_storage
    app.state.orchestration_runtime_host = runtime_host
    app.state.background_coordinator_loop = background_loop
    app.state.run_secret_store = run_secret_store
    app.state.swarm_run_service = SwarmRunService(resolved_settings, coordinator, run_secret_store)
    app.state.sandbox_client = sandbox_client
    app.state.sandbox_group_client = SandboxGroupManagementClient(
        resource_group=resolved_settings.azure.resource_group,
        subscription_id=resolved_settings.azure.subscription_id,
    )
    app.state.publish_service = publish_service

    app.include_router(health.router)
    app.include_router(swarm_runs.router)
    return app


app = create_app()


def create_runtime_app() -> FastAPI:
    return create_app(allow_placeholder_settings=False)
