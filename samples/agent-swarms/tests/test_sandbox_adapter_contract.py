from __future__ import annotations

import unittest

from contract_support import (
    call_log_tail_helper,
    call_redaction_helper,
    cleanup_scratch_dir,
    make_scratch_dir,
)


class SandboxAdapterContractTests(unittest.TestCase):
    def test_redaction_helper_strips_secret_values_but_keeps_diagnostics_actionable(self) -> None:
        secret = "ghp_supersecretvalue123456789"
        diagnostic = (
            "Worker exited with status 401.\n"
            f"stderr tail: GitHub token {secret} is invalid.\n"
            "stdout tail: starting reviewer sandbox"
        )

        redacted = call_redaction_helper(diagnostic)

        self.assertNotIn(secret, redacted)
        self.assertIn("401", redacted)
        self.assertIn("stderr tail", redacted.lower())
        self.assertIn("stdout tail", redacted.lower())
        self.assertNotEqual(redacted, diagnostic)

    def test_mirrored_log_tail_returns_only_new_content(self) -> None:
        scratch_dir = make_scratch_dir(self.id())
        try:
            log_path = scratch_dir / "logstream.log"
            first_chunk = "booting sandbox\nplanning started\n"
            second_chunk = "planning finished\n"

            log_path.write_text(first_chunk, encoding="utf-8")
            initial_size = log_path.stat().st_size
            cursor, first_result = call_log_tail_helper(log_path, offset=0)

            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(second_chunk)

            next_offset = cursor if cursor is not None else initial_size
            next_cursor, second_result = call_log_tail_helper(log_path, offset=next_offset)

            self.assertIn("booting sandbox", first_result)
            self.assertIn("planning started", first_result)
            self.assertIn("planning finished", second_result)
            self.assertNotIn("booting sandbox", second_result)
            self.assertNotIn("planning started", second_result)
            if next_cursor is not None:
                self.assertGreaterEqual(next_cursor, next_offset)
        finally:
            cleanup_scratch_dir(scratch_dir)


if __name__ == "__main__":
    unittest.main()
