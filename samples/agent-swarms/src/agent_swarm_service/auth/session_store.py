from __future__ import annotations

import asyncio
from datetime import UTC, datetime, timedelta
from typing import Any, Protocol

from pydantic import BaseModel, SecretStr


class JsonDocumentBackend(Protocol):
    @property
    def metadata(self) -> dict[str, Any]: ...

    async def write_json(self, path: str, payload: dict[str, Any]) -> None: ...

    async def read_json(self, path: str) -> dict[str, Any] | None: ...

    async def delete(self, path: str) -> None: ...

class GitHubRunSecret(BaseModel):
    run_id: str
    token: SecretStr
    created_at_utc: datetime
    expires_at_utc: datetime

    @property
    def environment(self) -> dict[str, str]:
        value = self.token.get_secret_value()
        return {
            "COPILOT_GITHUB_TOKEN": value,
            "GH_TOKEN": value,
            "GITHUB_TOKEN": value,
            "GIT_TERMINAL_PROMPT": "0",
            "GCM_INTERACTIVE": "Never",
        }


class RunSecretStore(Protocol):
    async def store(self, secret: GitHubRunSecret) -> None: ...

    async def get(self, run_id: str) -> GitHubRunSecret | None: ...

    async def delete(self, run_id: str) -> None: ...


class InMemoryRunSecretStore:
    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._secrets: dict[str, GitHubRunSecret] = {}

    async def store(self, secret: GitHubRunSecret) -> None:
        async with self._lock:
            self._secrets[secret.run_id] = secret

    async def get(self, run_id: str) -> GitHubRunSecret | None:
        async with self._lock:
            secret = self._secrets.get(run_id)
            if secret is None or secret.expires_at_utc <= datetime.now(UTC):
                self._secrets.pop(run_id, None)
                return None
            return secret

    async def delete(self, run_id: str) -> None:
        async with self._lock:
            self._secrets.pop(run_id, None)


class DurableRunSecretStore:
    def __init__(self, backend: JsonDocumentBackend) -> None:
        self._backend = backend

    async def store(self, secret: GitHubRunSecret) -> None:
        payload = secret.model_dump(mode="json")
        payload["token"] = secret.token.get_secret_value()
        await self._backend.write_json(self._path(secret.run_id), payload)

    async def get(self, run_id: str) -> GitHubRunSecret | None:
        payload = await self._backend.read_json(self._path(run_id))
        if payload is None:
            return None
        secret = GitHubRunSecret.model_validate(payload)
        if secret.expires_at_utc <= datetime.now(UTC):
            await self.delete(run_id)
            return None
        return secret

    async def delete(self, run_id: str) -> None:
        await self._backend.delete(self._path(run_id))

    @staticmethod
    def _path(run_id: str) -> str:
        return f"run-secrets/{run_id}.json"


def build_run_secret(
    run_id: str,
    token: str,
    *,
    lifetime: timedelta,
) -> GitHubRunSecret:
    now = datetime.now(UTC)
    return GitHubRunSecret(
        run_id=run_id,
        token=token,
        created_at_utc=now,
        expires_at_utc=now + lifetime,
    )
