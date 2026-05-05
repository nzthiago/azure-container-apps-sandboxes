from __future__ import annotations

from fastapi import APIRouter

from agent_swarm_service import __version__
from agent_swarm_service.api.schemas import HealthResponse

router = APIRouter(tags=["health"])


def _health_payload() -> HealthResponse:
    return HealthResponse(status="healthy", service="agent_swarm_service", version=__version__)


@router.get("/health", response_model=HealthResponse, include_in_schema=False)
async def get_health_root() -> HealthResponse:
    """Liveness/readiness probe path used by the Container App."""
    return _health_payload()


@router.get("/api/health", response_model=HealthResponse)
async def get_health() -> HealthResponse:
    """Public health endpoint, kept consistent with the rest of the `/api/...` surface."""
    return _health_payload()
