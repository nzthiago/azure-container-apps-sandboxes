from __future__ import annotations

import json
from typing import Any


def format_sse_event(event_type: str, payload: Any) -> bytes:
    data = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
    return f"event: {event_type}\ndata: {data}\n\n".encode("utf-8")
