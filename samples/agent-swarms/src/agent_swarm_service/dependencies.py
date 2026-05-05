from __future__ import annotations

from typing import Any

from fastapi import Request
from azure.containerapps.sandbox import SandboxGroupClient

from agent_swarm_service.config import ServiceSettings
from agent_swarm_service.sandboxes.logs import SandboxLogChunk, redact_text
from agent_swarm_service.sandboxes.workspace import read_bytes
from agent_swarm_service.services.swarm_runs import SwarmRunService


def get_settings(request: Request) -> ServiceSettings:
    return request.app.state.settings


def get_swarm_run_service(request: Request) -> SwarmRunService:
    return request.app.state.swarm_run_service


def get_sandbox_client(request: Request) -> Any:
    return request.app.state.sandbox_client


def get_sandbox_group_client(request: Request) -> SandboxGroupClient:
    return request.app.state.sandbox_group_client


async def read_log_chunk(
    sandbox_id: str,
    sandbox_group: str,
    path: str,
    *,
    offset: int,
    limit_bytes: int,
    sandbox_client: Any,
) -> SandboxLogChunk:
    content = await read_bytes(sandbox_client, sandbox_id, sandbox_group, path)
    chunk = content[offset : offset + limit_bytes]
    return SandboxLogChunk(
        offset=offset + len(chunk),
        content=redact_text(chunk.decode("utf-8", errors="replace")),
        is_truncated=offset + limit_bytes < len(content),
    )
