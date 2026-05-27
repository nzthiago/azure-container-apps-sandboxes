"""Control server for the computer-use desktop sandbox.

Exposes two FastAPI apps that run on different ports:

* ``app``       on :7000 — the computer-use primitives the agent loop calls
                          (screenshot, click, type, scroll, key, etc).
* ``form_app``  on :8080 — the demo expense-report form the agent fills out.

Splitting the ports keeps the demo URL the agent navigates to (``localhost:8080``)
cleanly separate from the operator-facing control channel.

The server runs **inside** the sandbox. The operator's agent loop runs outside
the sandbox and talks to ``app`` over the sandbox's public ``add_port(7000)``.
"""

from __future__ import annotations

import base64
import io
import json
import os
import shlex
import subprocess
from pathlib import Path
from typing import Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import HTMLResponse, JSONResponse, PlainTextResponse
from pydantic import BaseModel

DISPLAY = os.environ.get("DISPLAY", ":99")
FORM_DIR = Path(__file__).parent / "form"
SUBMISSION_PATH = Path("/tmp/submission.json")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _run(cmd: list[str], *, timeout: float = 10.0) -> str:
    env = {**os.environ, "DISPLAY": DISPLAY}
    proc = subprocess.run(
        cmd, env=env, capture_output=True, timeout=timeout, text=True,
    )
    if proc.returncode != 0:
        raise HTTPException(
            status_code=500,
            detail=f"{cmd[0]} failed: {proc.stderr.strip() or proc.stdout.strip()}",
        )
    return proc.stdout


def _xdotool(*args: str, timeout: float = 5.0) -> str:
    return _run(["xdotool", *args], timeout=timeout)


# OpenAI computer-use uses CSS-style button names; xdotool wants 1/2/3.
_BUTTON_MAP = {
    "left": "1", "middle": "2", "right": "3",
    "wheel_up": "4", "wheel_down": "5",
    "back": "8", "forward": "9",
    "1": "1", "2": "2", "3": "3",
}


def _button(name: Optional[str]) -> str:
    if not name:
        return "1"
    return _BUTTON_MAP.get(name.lower(), "1")


# Translate the symbolic key names OpenAI / Anthropic use to xdotool key names.
# This is intentionally a thin map — most printable keys pass through unchanged.
_KEY_MAP = {
    "enter": "Return", "return": "Return",
    "tab": "Tab", "space": "space", "backspace": "BackSpace",
    "delete": "Delete", "escape": "Escape", "esc": "Escape",
    "up": "Up", "down": "Down", "left": "Left", "right": "Right",
    "home": "Home", "end": "End", "pageup": "Page_Up", "pagedown": "Page_Down",
    "cmd": "super", "win": "super", "meta": "super",
    "ctrl": "ctrl", "control": "ctrl", "alt": "alt", "shift": "shift",
}


def _xkey(key: str) -> str:
    return _KEY_MAP.get(key.lower(), key)


# ---------------------------------------------------------------------------
# Control server (port 7000)
# ---------------------------------------------------------------------------

app = FastAPI(title="ACA sandbox computer-use control server")


@app.get("/healthz")
def healthz() -> dict:
    return {"ok": True, "display": DISPLAY}


@app.get("/screenshot")
def screenshot() -> dict:
    """PNG screenshot of the X display, base64-encoded."""
    # scrot writes to a path; we read it back. -o overwrites if exists.
    out = Path("/tmp/screen.png")
    if out.exists():
        out.unlink()
    _run(["scrot", "-o", str(out)], timeout=5.0)
    data = out.read_bytes()
    return {"image_base64": base64.b64encode(data).decode("ascii")}


class Point(BaseModel):
    x: int
    y: int


class ClickReq(BaseModel):
    x: int
    y: int
    button: Optional[str] = "left"


@app.post("/click")
def click(req: ClickReq) -> dict:
    _xdotool("mousemove", str(req.x), str(req.y))
    _xdotool("click", _button(req.button))
    return {"ok": True}


@app.post("/double_click")
def double_click(req: ClickReq) -> dict:
    _xdotool("mousemove", str(req.x), str(req.y))
    _xdotool("click", "--repeat", "2", "--delay", "75", _button(req.button))
    return {"ok": True}


class MoveReq(BaseModel):
    x: int
    y: int


@app.post("/move")
def move(req: MoveReq) -> dict:
    _xdotool("mousemove", str(req.x), str(req.y))
    return {"ok": True}


class DragReq(BaseModel):
    path: list[Point]
    button: Optional[str] = "left"


@app.post("/drag")
def drag(req: DragReq) -> dict:
    if len(req.path) < 2:
        raise HTTPException(status_code=400, detail="drag path needs >= 2 points")
    btn = _button(req.button)
    start = req.path[0]
    _xdotool("mousemove", str(start.x), str(start.y))
    _xdotool("mousedown", btn)
    try:
        for pt in req.path[1:]:
            _xdotool("mousemove", str(pt.x), str(pt.y))
    finally:
        _xdotool("mouseup", btn)
    return {"ok": True}


class TypeReq(BaseModel):
    text: str


@app.post("/type")
def type_text(req: TypeReq) -> dict:
    # --clearmodifiers prevents an accidental Shift being held from a prior keypress.
    # --delay 12ms keeps typing visible in the noVNC tab.
    _xdotool("type", "--clearmodifiers", "--delay", "12", req.text, timeout=30.0)
    return {"ok": True}


class KeyReq(BaseModel):
    # Either a single chord ("ctrl+l") or a list of keys ("ctrl", "l").
    keys: list[str]


@app.post("/key")
def key(req: KeyReq) -> dict:
    if not req.keys:
        raise HTTPException(status_code=400, detail="no keys")
    chord = "+".join(_xkey(k) for k in req.keys)
    _xdotool("key", "--clearmodifiers", chord)
    return {"ok": True}


class ScrollReq(BaseModel):
    x: int
    y: int
    scroll_x: int = 0
    scroll_y: int = 0


@app.post("/scroll")
def scroll(req: ScrollReq) -> dict:
    _xdotool("mousemove", str(req.x), str(req.y))
    # xdotool's wheel buttons: 4=up, 5=down, 6=left, 7=right.
    # OpenAI's `scroll_y` is positive=down (page scrolls down), so we click 5.
    # We click once per ~50px of scroll, capped at 20 ticks for sanity.
    def _ticks(amount: int) -> int:
        return max(0, min(20, abs(amount) // 50 or (1 if amount else 0)))
    for _ in range(_ticks(req.scroll_y)):
        _xdotool("click", "5" if req.scroll_y > 0 else "4")
    for _ in range(_ticks(req.scroll_x)):
        _xdotool("click", "7" if req.scroll_x > 0 else "6")
    return {"ok": True}


class WaitReq(BaseModel):
    ms: int = 500


@app.post("/wait")
def wait(req: WaitReq) -> dict:
    import time
    time.sleep(max(0, min(req.ms, 10_000)) / 1000.0)
    return {"ok": True}


@app.get("/submission")
def get_submission() -> JSONResponse:
    """Return the last form submission written by the form_app, if any."""
    if not SUBMISSION_PATH.exists():
        return JSONResponse({"submitted": False}, status_code=200)
    try:
        data = json.loads(SUBMISSION_PATH.read_text())
    except json.JSONDecodeError as e:
        raise HTTPException(status_code=500, detail=f"corrupt submission: {e}")
    return JSONResponse({"submitted": True, "data": data})


# ---------------------------------------------------------------------------
# Demo form server (port 8080)
# ---------------------------------------------------------------------------

form_app = FastAPI(title="Expense report demo form")


@form_app.get("/", response_class=HTMLResponse)
def index() -> str:
    return (FORM_DIR / "index.html").read_text(encoding="utf-8")


@form_app.get("/static/{name}")
def static_asset(name: str) -> PlainTextResponse:
    path = FORM_DIR / name
    if not path.is_file():
        raise HTTPException(status_code=404, detail="not found")
    return PlainTextResponse(path.read_text(encoding="utf-8"))


@form_app.post("/submit")
async def submit(request: Request) -> dict:
    payload = await request.json()
    SUBMISSION_PATH.write_text(json.dumps(payload, indent=2))
    return {"ok": True}


@form_app.get("/submission")
def form_submission() -> JSONResponse:
    return get_submission()
