from __future__ import annotations

from typing import Any, Mapping

from azure.sandbox import SandboxClient

_DEFAULT_WORKSPACE_ROOT = "/workspace"

SandboxHandle = dict[str, Any]


def attach_sandbox_context(
    sandbox: Mapping[str, Any],
    *,
    sandbox_group: str,
    resource_group: str | None,
    default_resource_group: str,
) -> SandboxHandle:
    return {
        **dict(sandbox),
        "sandbox_group": sandbox_group,
        "resource_group": resource_group or default_resource_group,
    }


def sandbox_id(sandbox: Mapping[str, Any]) -> str:
    return str(sandbox.get("id") or sandbox.get("name") or "")
