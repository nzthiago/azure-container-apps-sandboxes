"""Skill-side entry point for Durable Task Scheduler sandbox workflows.

This keeps the skill script aligned with the DTS lab implementation in
`labs/02-durable-task-workflows/main.py`.

Usage:
    python plugin/skills/azure-sandbox/scripts/durable-task-sandbox-workflows.py
    python plugin/skills/azure-sandbox/scripts/durable-task-sandbox-workflows.py --assign-current-user-role --stop-and-resume
"""

from __future__ import annotations

import runpy
from pathlib import Path


LAB_SCRIPT = Path(__file__).resolve().parents[4] / "labs" / "02-durable-task-workflows" / "main.py"


def main() -> None:
    if not LAB_SCRIPT.is_file():
        raise FileNotFoundError(f"Expected DTS lab script at {LAB_SCRIPT}")
    runpy.run_path(str(LAB_SCRIPT), run_name="__main__")


if __name__ == "__main__":
    main()
