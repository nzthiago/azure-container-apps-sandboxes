from __future__ import annotations

import asyncio
import copy
import json
import uuid
from collections.abc import Mapping
from datetime import timedelta
from typing import Any, Protocol
from urllib.parse import urlparse

from pydantic import BaseModel, Field

from agent_swarm_service.config import DurableStorageBackend, ServiceSettings
from agent_swarm_service.orchestration.models import RunLease, utcnow


class RuntimeStorageError(RuntimeError):
    """Raised when the configured runtime storage backend cannot be initialized."""


class QueueMessage(BaseModel):
    message_id: str
    payload: dict[str, Any]
    pop_receipt: str | None = None
    dequeue_count: int = 0


class RuntimeStorageBackendProtocol(Protocol):
    @property
    def metadata(self) -> Mapping[str, Any]: ...

    async def write_json(self, path: str, payload: Mapping[str, Any]) -> None: ...

    async def read_json(self, path: str) -> dict[str, Any] | None: ...

    async def delete(self, path: str) -> None: ...

    async def list_json(self, prefix: str) -> list[tuple[str, dict[str, Any]]]: ...

    async def enqueue(self, queue_name: str, payload: Mapping[str, Any]) -> None: ...

    async def dequeue(
        self,
        queue_name: str,
        *,
        visibility_timeout_seconds: int,
    ) -> QueueMessage | None: ...

    async def complete(self, queue_name: str, message: QueueMessage) -> None: ...

    async def acquire_lease(self, path: str, holder_id: str, lease_seconds: int) -> RunLease | None: ...

    async def renew_lease(self, path: str, lease: RunLease, lease_seconds: int) -> RunLease | None: ...

    async def release_lease(self, path: str, lease: RunLease) -> None: ...


class InMemoryRuntimeStorageBackend:
    def __init__(
        self,
        *,
        metadata: Mapping[str, Any] | None = None,
    ) -> None:
        self._lock = asyncio.Lock()
        self._documents: dict[str, dict[str, Any]] = {}
        self._queues: dict[str, list[dict[str, Any]]] = {}
        self._leases: dict[str, RunLease] = {}
        self._metadata = dict(
            metadata
            or {
                "backend": "memory",
                "limitation_code": "local-memory-only",
                "summary": "Explicit local fallback: state stays in-process and is lost on restart.",
                "is_durable": False,
                "supports_cross_instance_continuity": False,
            }
        )

    @property
    def metadata(self) -> Mapping[str, Any]:
        return self._metadata

    async def write_json(self, path: str, payload: Mapping[str, Any]) -> None:
        async with self._lock:
            self._documents[path] = copy.deepcopy(dict(payload))

    async def read_json(self, path: str) -> dict[str, Any] | None:
        async with self._lock:
            document = self._documents.get(path)
            return None if document is None else copy.deepcopy(document)

    async def delete(self, path: str) -> None:
        async with self._lock:
            self._documents.pop(path, None)
            self._leases.pop(path, None)

    async def list_json(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        async with self._lock:
            return [
                (path, copy.deepcopy(payload))
                for path, payload in sorted(self._documents.items())
                if path.startswith(prefix)
            ]

    async def enqueue(self, queue_name: str, payload: Mapping[str, Any]) -> None:
        async with self._lock:
            now = utcnow()
            self._queues.setdefault(queue_name, []).append(
                {
                    "message_id": uuid.uuid4().hex,
                    "payload": copy.deepcopy(dict(payload)),
                    "visible_at_utc": now,
                    "dequeue_count": 0,
                    "pop_receipt": None,
                }
            )

    async def dequeue(
        self,
        queue_name: str,
        *,
        visibility_timeout_seconds: int,
    ) -> QueueMessage | None:
        now = utcnow()
        async with self._lock:
            for message in self._queues.get(queue_name, []):
                if message["visible_at_utc"] > now:
                    continue
                pop_receipt = uuid.uuid4().hex
                message["visible_at_utc"] = now + timedelta(seconds=visibility_timeout_seconds)
                message["dequeue_count"] += 1
                message["pop_receipt"] = pop_receipt
                return QueueMessage(
                    message_id=message["message_id"],
                    payload=copy.deepcopy(message["payload"]),
                    pop_receipt=pop_receipt,
                    dequeue_count=message["dequeue_count"],
                )
        return None

    async def complete(self, queue_name: str, message: QueueMessage) -> None:
        async with self._lock:
            queue = self._queues.get(queue_name, [])
            self._queues[queue_name] = [
                item
                for item in queue
                if not (
                    item["message_id"] == message.message_id
                    and item.get("pop_receipt") == message.pop_receipt
                )
            ]

    async def acquire_lease(self, path: str, holder_id: str, lease_seconds: int) -> RunLease | None:
        now = utcnow()
        expires_at_utc = now + timedelta(seconds=lease_seconds)
        async with self._lock:
            existing = self._leases.get(path)
            if existing is not None and existing.expires_at_utc > now and existing.holder_id != holder_id:
                return None
            lease = RunLease(
                holder_id=holder_id,
                acquired_at_utc=now,
                heartbeat_at_utc=now,
                expires_at_utc=expires_at_utc,
                lease_token=holder_id,
            )
            self._leases[path] = lease
            return lease

    async def renew_lease(self, path: str, lease: RunLease, lease_seconds: int) -> RunLease | None:
        now = utcnow()
        async with self._lock:
            existing = self._leases.get(path)
            if existing is None or existing.holder_id != lease.holder_id or existing.expires_at_utc <= now:
                return None
            renewed = existing.model_copy(
                update={
                    "heartbeat_at_utc": now,
                    "expires_at_utc": now + timedelta(seconds=lease_seconds),
                }
            )
            self._leases[path] = renewed
            return renewed

    async def release_lease(self, path: str, lease: RunLease) -> None:
        async with self._lock:
            existing = self._leases.get(path)
            if existing is not None and existing.holder_id == lease.holder_id:
                self._leases.pop(path, None)

    def get_queue_messages(self, queue_name: str) -> list[dict[str, Any]]:
        return [copy.deepcopy(item["payload"]) for item in self._queues.get(queue_name, [])]


class AzureBlobQueueRuntimeStorageBackend:
    def __init__(self, settings: ServiceSettings) -> None:
        try:
            from azure.core.exceptions import HttpResponseError, ResourceExistsError, ResourceNotFoundError
            from azure.identity import DefaultAzureCredential
            from azure.storage.blob import BlobLeaseClient, BlobServiceClient
            from azure.storage.queue import QueueServiceClient
        except ModuleNotFoundError as exc:  # pragma: no cover - exercised via import-safe startup.
            raise RuntimeStorageError(
                "Azure Storage runtime requires azure-identity, azure-storage-blob, and azure-storage-queue."
            ) from exc

        self._BlobLeaseClient = BlobLeaseClient
        self._HttpResponseError = HttpResponseError
        self._ResourceExistsError = ResourceExistsError
        self._ResourceNotFoundError = ResourceNotFoundError
        self._container_name = settings.storage.container_name
        self._container_ready = False
        self._queue_names: set[str] = set()
        self._setup_lock = asyncio.Lock()
        self._metadata = {
            "backend": "azure-storage",
            "limitation_code": "none",
            "summary": "Auth continuity, GitHub tokens, pending OAuth state, and the run ownership index are stored in Azure Storage.",
            "is_durable": True,
            "supports_cross_instance_continuity": True,
        }

        connection_string = (
            settings.storage.connection_string.get_secret_value()
            if settings.storage.connection_string is not None
            else None
        )
        if connection_string:
            self._blob_service = BlobServiceClient.from_connection_string(connection_string)
            self._queue_service = QueueServiceClient.from_connection_string(connection_string)
        else:
            credential = DefaultAzureCredential()
            self._blob_service = BlobServiceClient(
                account_url=str(settings.azure.storage_account_url),
                credential=credential,
            )
            self._queue_service = QueueServiceClient(
                account_url=_derive_queue_service_url(str(settings.azure.storage_account_url)),
                credential=credential,
            )
        self._container_client = self._blob_service.get_container_client(self._container_name)

    @property
    def metadata(self) -> Mapping[str, Any]:
        return self._metadata

    async def write_json(self, path: str, payload: Mapping[str, Any]) -> None:
        await self._ensure_container()
        data = json.dumps(dict(payload), sort_keys=True).encode("utf-8")

        def _upload() -> None:
            self._container_client.upload_blob(name=path, data=data, overwrite=True)

        await asyncio.to_thread(_upload)

    async def read_json(self, path: str) -> dict[str, Any] | None:
        await self._ensure_container()
        blob_client = self._container_client.get_blob_client(path)

        def _download() -> dict[str, Any] | None:
            try:
                payload = blob_client.download_blob().readall()
            except self._ResourceNotFoundError:
                return None
            return json.loads(payload.decode("utf-8"))

        return await asyncio.to_thread(_download)

    async def delete(self, path: str) -> None:
        await self._ensure_container()
        blob_client = self._container_client.get_blob_client(path)

        def _delete() -> None:
            try:
                blob_client.delete_blob(delete_snapshots="include")
            except self._ResourceNotFoundError:
                return

        await asyncio.to_thread(_delete)

    async def list_json(self, prefix: str) -> list[tuple[str, dict[str, Any]]]:
        await self._ensure_container()

        def _list_names() -> list[str]:
            return [blob.name for blob in self._container_client.list_blobs(name_starts_with=prefix)]

        blob_names = await asyncio.to_thread(_list_names)
        results: list[tuple[str, dict[str, Any]]] = []
        for name in blob_names:
            payload = await self.read_json(name)
            if payload is not None:
                results.append((name, payload))
        return results

    async def enqueue(self, queue_name: str, payload: Mapping[str, Any]) -> None:
        queue_client = await self._ensure_queue(queue_name)
        message = json.dumps(dict(payload), sort_keys=True)
        await asyncio.to_thread(queue_client.send_message, message)

    async def dequeue(
        self,
        queue_name: str,
        *,
        visibility_timeout_seconds: int,
    ) -> QueueMessage | None:
        queue_client = await self._ensure_queue(queue_name)

        def _receive() -> QueueMessage | None:
            messages = list(queue_client.receive_messages(messages_per_page=1, visibility_timeout=visibility_timeout_seconds))
            if not messages:
                return None
            message = messages[0]
            return QueueMessage(
                message_id=message.id,
                payload=json.loads(message.content),
                pop_receipt=message.pop_receipt,
                dequeue_count=getattr(message, "dequeue_count", 1),
            )

        return await asyncio.to_thread(_receive)

    async def complete(self, queue_name: str, message: QueueMessage) -> None:
        if not message.pop_receipt:
            return
        queue_client = await self._ensure_queue(queue_name)
        await asyncio.to_thread(queue_client.delete_message, message.message_id, message.pop_receipt)

    async def acquire_lease(self, path: str, holder_id: str, lease_seconds: int) -> RunLease | None:
        blob_client = await self._ensure_lease_blob(path)
        lease_client = self._BlobLeaseClient(blob_client)
        now = utcnow()

        try:
            lease_token = await asyncio.to_thread(lease_client.acquire, lease_duration=lease_seconds)
        except self._HttpResponseError as exc:
            if "lease" in str(exc).lower():
                return None
            raise

        return RunLease(
            holder_id=holder_id,
            acquired_at_utc=now,
            heartbeat_at_utc=now,
            expires_at_utc=now + timedelta(seconds=lease_seconds),
            lease_token=str(lease_token),
        )

    async def renew_lease(self, path: str, lease: RunLease, lease_seconds: int) -> RunLease | None:
        if not lease.lease_token:
            return None
        blob_client = await self._ensure_lease_blob(path)
        lease_client = self._BlobLeaseClient(blob_client, lease.lease_token)
        try:
            await asyncio.to_thread(lease_client.renew)
        except self._HttpResponseError as exc:
            if "lease" in str(exc).lower():
                return None
            raise
        now = utcnow()
        return lease.model_copy(
            update={
                "heartbeat_at_utc": now,
                "expires_at_utc": now + timedelta(seconds=lease_seconds),
            }
        )

    async def release_lease(self, path: str, lease: RunLease) -> None:
        if not lease.lease_token:
            return
        blob_client = await self._ensure_lease_blob(path)
        lease_client = self._BlobLeaseClient(blob_client, lease.lease_token)
        try:
            await asyncio.to_thread(lease_client.release)
        except self._HttpResponseError as exc:
            if "lease" in str(exc).lower():
                return
            raise

    async def _ensure_container(self) -> None:
        if self._container_ready:
            return
        async with self._setup_lock:
            if self._container_ready:
                return

            def _create() -> None:
                try:
                    self._container_client.create_container()
                except self._ResourceExistsError:
                    return

            await asyncio.to_thread(_create)
            self._container_ready = True

    async def _ensure_queue(self, queue_name: str):
        if queue_name in self._queue_names:
            return self._queue_service.get_queue_client(queue_name)
        async with self._setup_lock:
            queue_client = self._queue_service.get_queue_client(queue_name)
            if queue_name not in self._queue_names:

                def _create() -> None:
                    try:
                        queue_client.create_queue()
                    except self._ResourceExistsError:
                        return

                await asyncio.to_thread(_create)
                self._queue_names.add(queue_name)
            return queue_client

    async def _ensure_lease_blob(self, path: str):
        await self._ensure_container()
        blob_client = self._container_client.get_blob_client(path)

        def _create() -> None:
            try:
                blob_client.upload_blob(b"{}", overwrite=False)
            except self._ResourceExistsError:
                return

        await asyncio.to_thread(_create)
        return blob_client


def create_runtime_storage(settings: ServiceSettings) -> RuntimeStorageBackendProtocol:
    if settings.storage.backend is DurableStorageBackend.MEMORY:
        return InMemoryRuntimeStorageBackend()
    return AzureBlobQueueRuntimeStorageBackend(settings)


def _derive_queue_service_url(blob_account_url: str) -> str:
    parsed = urlparse(blob_account_url)
    if ".blob." not in parsed.netloc:
        raise RuntimeStorageError(
            "SWARM_STORAGE_ACCOUNT_URL must point at the blob service endpoint when no connection string is supplied."
        )
    queue_netloc = parsed.netloc.replace(".blob.", ".queue.", 1)
    return f"{parsed.scheme}://{queue_netloc}"
