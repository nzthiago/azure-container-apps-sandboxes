from __future__ import annotations

import asyncio
import json
from typing import Any

from pydantic import BaseModel, Field

DEFAULT_WORKSPACE_ROOT = "/workspace"
DEFAULT_LOG_MIRROR_PATH = f"{DEFAULT_WORKSPACE_ROOT}/.swarm/logstream.log"


def _is_already_exists_error(exc: Exception) -> bool:
    message = str(exc).lower()
    return "already exists" in message and "directory" in message


class WorkspaceFile(BaseModel):
    path: str
    content: str | bytes


class WorkspaceSnapshot(BaseModel):
    root: str = DEFAULT_WORKSPACE_ROOT
    files: list[WorkspaceFile] = Field(default_factory=list)


async def ensure_workspace(
    sandbox_client: Any,
    sandbox_id: str,
    sandbox_group: str,
    *,
    resource_group: str | None = None,
    root: str = DEFAULT_WORKSPACE_ROOT,
) -> None:
    for path in (root, f"{root}/.swarm"):
        try:
            await asyncio.to_thread(
                sandbox_client.mkdir,
                sandbox_id,
                sandbox_group,
                path,
                resource_group=resource_group,
            )
        except Exception as exc:
            if not _is_already_exists_error(exc):
                raise


async def stage_snapshot(
    sandbox_client: Any,
    sandbox_id: str,
    sandbox_group: str,
    snapshot: WorkspaceSnapshot,
    *,
    resource_group: str | None = None,
) -> None:
    await ensure_workspace(
        sandbox_client,
        sandbox_id,
        sandbox_group,
        resource_group=resource_group,
        root=snapshot.root,
    )
    for item in snapshot.files:
        destination = item.path if item.path.startswith("/") else f"{snapshot.root}/{item.path}"
        await asyncio.to_thread(
            sandbox_client.write_file,
            sandbox_id,
            sandbox_group,
            destination,
            item.content,
            resource_group=resource_group,
        )


async def read_bytes(
    sandbox_client: Any,
    sandbox_id: str,
    sandbox_group: str,
    path: str,
    *,
    resource_group: str | None = None,
) -> bytes:
    return await asyncio.to_thread(
        sandbox_client.read_file,
        sandbox_id,
        sandbox_group,
        path,
        resource_group=resource_group,
    )


async def read_text(
    sandbox_client: Any,
    sandbox_id: str,
    sandbox_group: str,
    path: str,
    *,
    resource_group: str | None = None,
) -> str:
    content = await read_bytes(
        sandbox_client,
        sandbox_id,
        sandbox_group,
        path,
        resource_group=resource_group,
    )
    return content.decode("utf-8", errors="replace")


async def read_json(
    sandbox_client: Any,
    sandbox_id: str,
    sandbox_group: str,
    path: str,
    *,
    resource_group: str | None = None,
) -> dict[str, Any]:
    return json.loads(
        await read_text(
            sandbox_client,
            sandbox_id,
            sandbox_group,
            path,
            resource_group=resource_group,
        )
    )
