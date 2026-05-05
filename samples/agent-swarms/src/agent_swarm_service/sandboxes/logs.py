from __future__ import annotations

import re
from pathlib import Path

from pydantic import BaseModel

SECRET_KEY_MARKERS = ("token", "secret", "password", "key")
BEARER_PATTERN = re.compile(r"Bearer\s+[A-Za-z0-9\-._~+/]+=*", re.IGNORECASE)
GITHUB_TOKEN_PATTERN = re.compile(
    r"\b(?:gh[pousr]_[A-Za-z0-9_]{8,}|github_pat_[A-Za-z0-9_]{20,})\b",
    re.IGNORECASE,
)
ASSIGNMENT_PATTERN = re.compile(
    r"(?P<key>[A-Za-z0-9_]*(?:token|secret|password|key)[A-Za-z0-9_]*)=(?P<value>[^\s]+)",
    re.IGNORECASE,
)


def redact_secret_value(value: str) -> str:
    if len(value) <= 8:
        return "***"
    return f"{value[:4]}...{value[-4:]}"


def redact_mapping(values: dict[str, str] | None) -> dict[str, str]:
    if not values:
        return {}
    return {
        key: redact_secret_value(value) if any(marker in key.lower() for marker in SECRET_KEY_MARKERS) else value
        for key, value in values.items()
    }


def redact_text(text: str, extra_values: list[str] | None = None) -> str:
    redacted = BEARER_PATTERN.sub("Bearer ***", text)
    redacted = GITHUB_TOKEN_PATTERN.sub(lambda match: redact_secret_value(match.group(0)), redacted)
    redacted = ASSIGNMENT_PATTERN.sub(lambda match: f"{match.group('key')}=***", redacted)
    for value in extra_values or []:
        if value:
            redacted = redacted.replace(value, redact_secret_value(value))
    return redacted


class SandboxLogChunk(BaseModel):
    offset: int
    content: str
    is_truncated: bool = False


def tail_mirrored_log(path: str | Path, offset: int = 0, encoding: str = "utf-8") -> tuple[int, str]:
    log_path = Path(path)
    if not log_path.exists():
        return 0, ""

    content = log_path.read_bytes()
    start = max(0, min(offset, len(content)))
    return len(content), content[start:].decode(encoding, errors="replace")
