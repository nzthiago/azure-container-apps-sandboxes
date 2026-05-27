"""ACAAsyncComputer: an `AsyncComputer` impl backed by an ACA sandbox.

Implements the openai-agents ``AsyncComputer`` interface by forwarding
every action to the FastAPI control server running on port 7000 inside
the sandbox (see ``desktop-image/control_server.py``).

That keeps the AI loop (this process) and the desktop (the sandbox)
cleanly separated by an HTTP boundary, mirroring the OpenAI cookbook's
Daytona example. Swapping ACA for any other VM/sandbox provider is then
just a different ``base_url``.
"""

from __future__ import annotations

import asyncio

import httpx
from agents import AsyncComputer
from agents.computer import Button, Environment


class ACAAsyncComputer(AsyncComputer):
    """An AsyncComputer backed by the in-sandbox control server."""

    def __init__(
        self,
        base_url: str,
        width: int = 1280,
        height: int = 800,
        request_timeout: float = 30.0,
    ) -> None:
        self._base_url = base_url.rstrip("/")
        self._width = width
        self._height = height
        self._client = httpx.AsyncClient(timeout=request_timeout)

    @property
    def environment(self) -> Environment | None:
        # The Responses API computer tool accepts: mac, windows, ubuntu, browser.
        # Our sandbox boots Ubuntu, so report that.
        return "ubuntu"

    @property
    def dimensions(self) -> tuple[int, int] | None:
        return (self._width, self._height)

    async def wait_until_ready(self, attempts: int = 30, delay: float = 1.0) -> None:
        """Poll /healthz until the control server is reachable.

        ACA-minted ingress URLs sometimes take a few seconds to propagate
        after add_port, so we don't want to start the agent loop too soon.
        """
        for _ in range(attempts):
            try:
                r = await self._client.get(f"{self._base_url}/healthz")
                if r.status_code == 200:
                    return
            except httpx.HTTPError:
                pass
            await asyncio.sleep(delay)
        raise RuntimeError(f"control server never became ready: {self._base_url}")

    async def close(self) -> None:
        await self._client.aclose()

    async def __aenter__(self) -> "ACAAsyncComputer":
        await self.wait_until_ready()
        return self

    async def __aexit__(self, *exc) -> None:
        await self.close()

    async def screenshot(self) -> str:
        r = await self._client.get(f"{self._base_url}/screenshot")
        r.raise_for_status()
        data = r.json()
        return data["image_base64"]

    async def click(
        self,
        x: int,
        y: int,
        button: Button = "left",
        *,
        keys: list[str] | None = None,
    ) -> None:
        body: dict = {"x": x, "y": y, "button": button}
        if keys:
            body["keys"] = keys
        await self._post("/click", body)

    async def double_click(
        self, x: int, y: int, *, keys: list[str] | None = None
    ) -> None:
        body: dict = {"x": x, "y": y}
        if keys:
            body["keys"] = keys
        await self._post("/double_click", body)

    async def scroll(self, x: int, y: int, scroll_x: int, scroll_y: int) -> None:
        await self._post(
            "/scroll",
            {"x": x, "y": y, "scroll_x": scroll_x, "scroll_y": scroll_y},
        )

    async def type(self, text: str) -> None:
        await self._post("/type", {"text": text})

    async def wait(self) -> None:
        await self._post("/wait", {"ms": 500})

    async def move(self, x: int, y: int) -> None:
        await self._post("/move", {"x": x, "y": y})

    async def keypress(self, keys: list[str]) -> None:
        await self._post("/key", {"keys": keys})

    async def drag(self, path: list[tuple[int, int]]) -> None:
        # Control server expects [{"x": ..., "y": ...}, ...] (see DragReq).
        await self._post("/drag", {"path": [{"x": x, "y": y} for x, y in path]})

    async def _post(self, path: str, body: dict) -> None:
        r = await self._client.post(f"{self._base_url}{path}", json=body)
        r.raise_for_status()

    async def fetch_submission(self) -> dict | None:
        """Returns the form's last submission payload, or None if not submitted."""
        r = await self._client.get(f"{self._base_url}/submission")
        if r.status_code == 404:
            return None
        r.raise_for_status()
        body = r.json()
        # Control server shape: {"submitted": bool, "data": {...}} when present,
        # or {"submitted": False} when /tmp/submission.json doesn't exist yet.
        if not body.get("submitted"):
            return None
        return body.get("data")
