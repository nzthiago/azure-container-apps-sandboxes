from __future__ import annotations

import argparse
import io
import runpy
import sys
import traceback
from contextlib import redirect_stderr, redirect_stdout
from pathlib import Path

BAKED_COPILOT_RUNTIME_PATH = Path("/opt/agent-swarm/copilot_runtime.py")


class Tee(io.TextIOBase):
    def __init__(self, *streams: io.TextIOBase) -> None:
        self._streams = streams

    def write(self, data: str) -> int:
        for stream in self._streams:
            stream.write(data)
            stream.flush()
        return len(data)

    def flush(self) -> None:
        for stream in self._streams:
            stream.flush()


def main() -> int:
    parser = argparse.ArgumentParser(description="Run the baked Agent Swarm sandbox contract.")
    parser.add_argument("--role", choices=("planner", "worker", "reviewer", "merge"), required=True)
    parser.add_argument("--workspace", required=True)
    parser.add_argument("--swarm-root", required=True)
    args = parser.parse_args()

    swarm_root = Path(args.swarm_root)
    swarm_root.mkdir(parents=True, exist_ok=True)
    request_file = swarm_root / "request.json"
    result_file = swarm_root / "result.json"
    log_file = swarm_root / "logstream.log"
    if not BAKED_COPILOT_RUNTIME_PATH.exists():
        print(f"Missing baked Copilot runtime at '{BAKED_COPILOT_RUNTIME_PATH}'.", file=sys.stderr)
        return 127
    if not request_file.exists():
        print(f"Missing sandbox request payload at '{request_file}'.", file=sys.stderr)
        return 1

    argv = [
        str(BAKED_COPILOT_RUNTIME_PATH),
        "--role",
        args.role,
        "--request",
        str(request_file),
        "--result",
        str(result_file),
        "--workspace",
        args.workspace,
    ]
    exit_code = 0
    with log_file.open("a", encoding="utf-8", buffering=1) as log:
        stdout = Tee(sys.stdout, log)
        stderr = Tee(sys.stderr, log)
        original_argv = sys.argv[:]
        try:
            sys.argv = argv
            with redirect_stdout(stdout), redirect_stderr(stderr):
                runpy.run_path(str(BAKED_COPILOT_RUNTIME_PATH), run_name="__main__")
        except SystemExit as exc:
            if isinstance(exc.code, int):
                exit_code = exc.code
            elif exc.code is None:
                exit_code = 0
            else:
                print(exc.code, file=stderr)
                exit_code = 1
        except Exception:
            traceback.print_exc(file=stderr)
            exit_code = 1
        finally:
            sys.argv = original_argv
    return exit_code


if __name__ == "__main__":
    raise SystemExit(main())
