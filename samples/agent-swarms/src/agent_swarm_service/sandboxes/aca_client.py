from __future__ import annotations

from dataclasses import asdict, is_dataclass
from typing import Any, Mapping

from azure.containerapps.sandbox import Sandbox, SandboxClient

_DEFAULT_WORKSPACE_ROOT = "/workspace"

SandboxSource = Sandbox | Mapping[str, Any]
SandboxHandle = dict[str, Any]


def _normalize_sandbox(sandbox: SandboxSource) -> SandboxHandle:
    if is_dataclass(sandbox):
        return asdict(sandbox)
    return dict(sandbox)


def attach_sandbox_context(
    sandbox: SandboxSource,
    *,
    sandbox_group: str,
    resource_group: str | None,
    default_resource_group: str,
) -> SandboxHandle:
    return {
        **_normalize_sandbox(sandbox),
        "sandbox_group": sandbox_group,
        "resource_group": resource_group or default_resource_group,
    }


def sandbox_id(sandbox: SandboxSource) -> str:
    if isinstance(sandbox, Mapping):
        return str(sandbox.get("id") or sandbox.get("name") or "")
    return str(getattr(sandbox, "id", "") or getattr(sandbox, "name", "") or "")
