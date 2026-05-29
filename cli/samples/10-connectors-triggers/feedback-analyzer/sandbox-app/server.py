"""Feedback-analyzer listener — runs inside the sandbox.

End-to-end round-trip for the ``10-connectors-triggers`` scenario:

  1. The Office 365 ``OnNewEmailV3`` trigger POSTs each new "Feedback"
     email to ``POST /webhook`` here.
  2. We reply 200 immediately (so the connector doesn't retry while we
     work) and hand the payload to a worker thread.
  3. The worker invokes the GitHub Copilot CLI (``copilot -p ...``)
     pre-installed on the ``copilot`` disk image to compose a friendly,
     personalised acknowledgment, then POSTs that reply back through
     the **same** Office 365 connection's ``/v2/Mail`` endpoint
     (SendMailV2). No ``Authorization`` header — the platform proxy
     injects a Bearer token from the sandbox-group managed identity
     based on the sandbox's ``gatewayConnections[]`` wiring (set up
     declaratively by ``run.py`` + ``setup/setup.py``).

Stdlib only on the HTTP side: ``http.server`` + ``urllib`` + ``ssl``.
The Copilot CLI is invoked via ``subprocess`` and must already be
authenticated (``run.py`` runs ``copilot login`` once at startup).

Loop-safety:
  The trigger fires on inbox subjects containing ``Feedback``. The reply
  email's subject must NEVER contain that word, otherwise the connector
  re-triggers on our own outbound mail and you get an infinite loop.
  See ``REPLY_SUBJECT_PREFIX`` + ``_safe_reply_subject`` below.

Environment:
  PORT                    listener port (default 5000)
  TRIAGE_RECIPIENT        where the acknowledgment email goes
  O365_RUNTIME_URL        connection's runtime URL (no trailing /)
  COPILOT_BIN             override path to the copilot CLI (default: ``copilot``)
  COPILOT_TIMEOUT_SECONDS max seconds to wait for a copilot reply (default 120)
"""
from __future__ import annotations

import datetime
import html as html_lib
import json
import os
import re
import shutil
import ssl
import subprocess
import sys
import threading
import traceback
import urllib.error
import urllib.parse
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

# ---------------------------------------------------------------------------
# SSL: the sandbox egress proxy uses TLS interception. stdlib ssl honors
# /etc/ssl/certs/ca-certificates.crt by default on ubuntu, but be explicit
# (and set SSL_CERT_FILE too in case downstream urllib paths reload).
# ---------------------------------------------------------------------------
CA_BUNDLE = "/etc/ssl/certs/ca-certificates.crt"
os.environ.setdefault("SSL_CERT_FILE", CA_BUNDLE)
_SSL_CTX = ssl.create_default_context(cafile=CA_BUNDLE if os.path.exists(CA_BUNDLE) else None)

PORT = int(os.environ.get("PORT", "5000"))
RUNTIME_URL = os.environ.get("O365_RUNTIME_URL", "").rstrip("/")
TRIAGE_RECIPIENT = os.environ.get("TRIAGE_RECIPIENT", "").strip()

COPILOT_BIN = os.environ.get("COPILOT_BIN", "copilot")
COPILOT_TIMEOUT_SECONDS = int(os.environ.get("COPILOT_TIMEOUT_SECONDS", "120"))
# How much of the incoming email body to feed to the model. The Copilot
# CLI handles long inputs fine, but we don't need the entire signature
# block / quoted history to compose a helpful reply.
COPILOT_INPUT_MAX_CHARS = int(os.environ.get("COPILOT_INPUT_MAX_CHARS", "4000"))

REPLY_SUBJECT_PREFIX = "Auto-ack"  # MUST NOT contain "Feedback"


# ---------- helpers ---------------------------------------------------------

def _strip_html(s: str) -> str:
    return html_lib.unescape(re.sub(r"<[^>]+>", " ", s)).strip()


def _safe_reply_subject(original_subject: str) -> str:
    """Build a reply subject that cannot retrigger ``subjectFilter=Feedback``.

    We use a fixed prefix and deliberately omit the original subject (which
    by design contains "Feedback" — that's how this trigger fired). The
    original subject is echoed in the reply body.
    """
    return f"{REPLY_SUBJECT_PREFIX}: received your message"


def _post_json(url: str, body: dict, *, headers: dict[str, str], timeout: int = 30
               ) -> tuple[int, bytes]:
    """POST JSON via urllib; return (status_code, response_body_bytes)."""
    data = json.dumps(body).encode("utf-8")
    req = urllib.request.Request(
        url, data=data, method="POST",
        headers={"Content-Type": "application/json", **headers},
    )
    try:
        with urllib.request.urlopen(req, context=_SSL_CTX, timeout=timeout) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read() if exc.fp is not None else b""


_COPILOT_PROMPT_TEMPLATE = (
    "You are a friendly customer-support agent for a small product team. "
    "A customer just emailed the following feedback. Compose a warm, "
    "professional reply (2 short paragraphs, plain prose, no markdown, "
    "no preamble like 'Sure, here is the reply'). Acknowledge the "
    "specific points they raised, thank them, and tell them you've "
    "logged the feedback for the team. Keep it under 150 words. Do NOT "
    "use the word 'Feedback' in the reply (it is a reserved keyword in "
    "our routing system).\n\n"
    "Customer:\n"
    "  From:    {sender}\n"
    "  Subject: {subject}\n\n"
    "Message:\n{body}\n\n"
    "Write only the reply text — no subject line, no signature."
)

# Serialize copilot CLI invocations so a burst of incoming emails doesn't
# spawn N concurrent CLI processes on this 2-CPU / 4-GiB sandbox. One in
# flight at a time is plenty for a demo and avoids resource thrash.
_COPILOT_LOCK = threading.Semaphore(1)


def _compose_reply(*, sender: str, subject: str, body_text: str
                   ) -> tuple[str, str | None]:
    """Invoke the Copilot CLI to generate a reply for ``body_text``.

    Returns ``(reply_text, error)``. On success ``error`` is ``None``.
    On failure ``reply_text`` is a generic fallback so the round-trip
    still completes and ``error`` carries diagnostics for the log.
    """
    if shutil.which(COPILOT_BIN) is None and not os.path.isabs(COPILOT_BIN):
        return (
            "Thank you for your message — a human will get back to you shortly.",
            f"copilot binary {COPILOT_BIN!r} not found on PATH",
        )

    truncated = body_text[:COPILOT_INPUT_MAX_CHARS]
    if len(body_text) > COPILOT_INPUT_MAX_CHARS:
        truncated += "\n…[truncated]"
    prompt = _COPILOT_PROMPT_TEMPLATE.format(
        sender=sender or "(unknown sender)",
        subject=subject or "(no subject)",
        body=truncated or "(empty body)",
    )

    with _COPILOT_LOCK:
        try:
            # Args are passed directly (no shell), so newlines + special
            # characters in ``prompt`` are safe. Argv length is bounded
            # by COPILOT_INPUT_MAX_CHARS (default 4 KiB) which is well
            # under ARG_MAX on Linux (typically 128 KiB+).
            #   ``-s``                silent (response only, no stats)
            #   ``--allow-all-tools`` don't prompt for tool consent
            # The banner is OFF by default in non-interactive mode, so
            # we don't pass ``--banner`` (and the CLI rejects
            # ``--no-banner`` outright).
            proc = subprocess.run(
                [COPILOT_BIN, "-p", prompt, "-s", "--allow-all-tools"],
                capture_output=True, text=True,
                timeout=COPILOT_TIMEOUT_SECONDS,
            )
        except subprocess.TimeoutExpired:
            return (
                "Thank you for your message — a human will get back to you shortly.",
                f"copilot CLI timed out after {COPILOT_TIMEOUT_SECONDS}s",
            )
        except Exception as exc:
            return (
                "Thank you for your message — a human will get back to you shortly.",
                f"copilot CLI failed to launch: {exc}",
            )

    reply = (proc.stdout or "").strip()
    if proc.returncode != 0 or not reply:
        return (
            "Thank you for your message — a human will get back to you shortly.",
            (
                f"copilot CLI exit={proc.returncode}, "
                f"stderr={(proc.stderr or '').strip()[:300]!r}"
            ),
        )

    # Strip the loop-guard word defensively in case the model ignored
    # the instruction.
    reply = re.sub(r"\bFeedback\b", "your message", reply)
    return reply, None


def _send_reply(to_addr: str, original_subject: str, original_from: str,
                received: str, ai_reply: str, ai_error: str | None) -> None:
    """POST a Copilot-composed acknowledgment email to the Office 365
    SendMailV2 action.

    No ``Authorization`` header — the platform proxy injects a Bearer
    token from the sandbox-group MI based on the sandbox's
    ``gatewayConnections[]`` wiring (set up at sandbox-create time by
    ``run.py`` / ``run.sh`` and declared on the sandbox group by
    ``setup/setup.py``).
    """
    if not RUNTIME_URL:
        raise RuntimeError("O365_RUNTIME_URL is not set")

    # Render the AI reply as a single HTML block — preserve paragraph
    # breaks, escape any HTML-significant characters.
    paragraphs = [p.strip() for p in re.split(r"\n\s*\n", ai_reply) if p.strip()]
    if not paragraphs:
        paragraphs = [ai_reply]
    body_paragraphs = "".join(
        f"<p>{html_lib.escape(p).replace(chr(10), '<br>')}</p>"
        for p in paragraphs
    )

    footer = (
        "<hr><p style='color:#888;font-size:12px'>"
        "Composed by GitHub Copilot CLI running inside an Azure Container "
        "Apps sandbox in response to your incoming email."
        "</p>"
    )
    if ai_error:
        footer += (
            "<p style='color:#a00;font-size:12px'>"
            "<b>Note:</b> the model call failed — sending a fallback "
            f"acknowledgment. (Diagnostic: {html_lib.escape(ai_error)})"
            "</p>"
        )

    body_html = (
        "<html><body style='font-family:sans-serif;max-width:640px'>"
        f"{body_paragraphs}"
        "<hr><h3 style='color:#666'>Your original message</h3>"
        f"<p><b>From:</b> {html_lib.escape(str(original_from))}<br>"
        f"<b>Subject:</b> {html_lib.escape(str(original_subject))}<br>"
        f"<b>Received:</b> {html_lib.escape(str(received))}</p>"
        f"{footer}"
        "</body></html>"
    )

    payload = {
        "To": to_addr,
        "Subject": _safe_reply_subject(original_subject),
        "Body": body_html,
        "Importance": "Normal",
        "IsHtml": True,
    }
    status, raw = _post_json(f"{RUNTIME_URL}/v2/Mail", payload, headers={})
    if status == 401:
        # 401 missing-authorization-header from the platform proxy means
        # the sandbox's outbound call wasn't intercepted by the
        # gateway-connections middleware (which would otherwise inject
        # Authorization: Bearer <SG-MI-token>). The single most common
        # cause is a stale runtime URL on the sandbox group's
        # gatewayConnections[] entry — typically because the connection
        # was re-created since the SG was wired. This sandbox can't fix
        # that itself; the operator has to re-run scenario setup from
        # the host. Log loudly so the cause is obvious in the listener
        # log, then surface the original error to the caller.
        sys.stderr.write(
            "[error] SendMailV2 returned 401 missing-authorization-header.\n"
            "[error] This sandbox's gatewayConnections wiring is not delivering\n"
            "[error] a Bearer token on outbound calls to the runtime URL.\n"
            "[error] Most likely the connection was re-created since the\n"
            "[error] sandbox group was wired, and the SG-level\n"
            "[error] gatewayConnections[] entry now points at a stale\n"
            "[error] connectionRuntimeUrl. Stop this listener (Enter in the\n"
            "[error] host run.sh / run.py) and re-run feedback-analyzer; its\n"
            "[error] preflight + setup will repair the wiring before the next\n"
            "[error] sandbox boots.\n"
        )
        sys.stderr.flush()
    if status not in (200, 202):
        raise RuntimeError(f"SendMailV2 HTTP {status}: {raw[:300]!r}")


# ---------- request parsing ------------------------------------------------

def _extract_emails(payload: object) -> list[dict]:
    """Normalize the various Office 365 webhook payload shapes into a list of
    email dicts. The connector sometimes wraps the payload as ``{"body": ...}``
    and sometimes ships the email object at the root; lists arrive under
    ``value`` (Graph-style) or as bare arrays.
    """
    body = payload.get("body", payload) if isinstance(payload, dict) else payload
    if isinstance(body, dict) and "value" in body:
        items = body["value"]
    elif isinstance(body, list):
        items = body
    elif isinstance(body, dict) and "subject" in body:
        items = [body]
    else:
        items = [body] if body else []

    out: list[dict] = []
    for item in items:
        if isinstance(item, dict):
            out.append(item)
        else:
            out.append({"subject": "(non-dict payload)", "body": str(item)})
    return out


def _process_one(email: dict) -> None:
    subject = str(email.get("subject", "(no subject)"))

    # Loop guard: never react to our own outbound replies.
    if REPLY_SUBJECT_PREFIX in subject:
        print(f"[skip] looks like our own reply: {subject!r}", flush=True)
        return

    from_addr = email.get("from", "unknown")
    if isinstance(from_addr, dict):
        from_addr = (from_addr.get("emailAddress", {}) or {}).get("address") \
            or from_addr.get("address") or str(from_addr)
    from_addr = str(from_addr)

    received = str(email.get(
        "receivedDateTime",
        datetime.datetime.now(datetime.timezone.utc).isoformat(),
    ))

    body_raw = email.get("body") or email.get("bodyPreview") or ""
    if isinstance(body_raw, dict):
        body_raw = body_raw.get("content") or body_raw.get("contentText") or ""
    body_text = _strip_html(str(body_raw))

    print(
        f"[in ] from={from_addr!r} subject={subject!r} "
        f"chars={len(body_text)} — invoking copilot CLI...",
        flush=True,
    )
    ai_reply, ai_error = _compose_reply(
        sender=from_addr, subject=subject, body_text=body_text,
    )
    if ai_error:
        print(f"[ai ] error: {ai_error}", flush=True)
    else:
        preview = ai_reply.replace("\n", " ")[:120]
        print(f"[ai ] {len(ai_reply)} chars: {preview!r}", flush=True)

    _send_reply(TRIAGE_RECIPIENT, subject, from_addr, received,
                ai_reply, ai_error)
    print(f"[out] ack sent to {TRIAGE_RECIPIENT}", flush=True)


def _process_batch(items: list[dict]) -> None:
    for item in items:
        try:
            _process_one(item)
        except Exception:
            print("[err] processing failed:\n" + traceback.format_exc(),
                  file=sys.stderr, flush=True)


# ---------- HTTP handler ---------------------------------------------------

class Handler(BaseHTTPRequestHandler):

    def _send(self, code: int, body: dict) -> None:
        data = json.dumps(body).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def do_POST(self):
        length = int(self.headers.get("Content-Length", "0") or "0")
        raw = self.rfile.read(length) if length else b""
        try:
            payload = json.loads(raw or b"{}")
        except json.JSONDecodeError:
            payload = {"raw": raw.decode("utf-8", "replace")}

        items = _extract_emails(payload)
        # Ack immediately so the connector doesn't retry while we process.
        self._send(200, {"status": "received", "count": len(items)})

        if items:
            threading.Thread(
                target=_process_batch, args=(items,), daemon=True,
            ).start()

    def do_GET(self):
        if self.path.rstrip("/") in ("/healthz", ""):
            self._send(200, {"status": "ok"})
            return
        self._send(404, {"status": "not found", "path": self.path})

    def log_message(self, fmt: str, *args) -> None:
        sys.stderr.write(
            "%s - %s\n" % (self.address_string(), fmt % args)
        )


# ---------- entrypoint -----------------------------------------------------

def _required_env_or_exit() -> None:
    missing = []
    if not RUNTIME_URL:       missing.append("O365_RUNTIME_URL")
    if not TRIAGE_RECIPIENT:  missing.append("TRIAGE_RECIPIENT")
    if missing:
        print(f"error: required env vars not set: {missing}", file=sys.stderr)
        sys.exit(2)


def main() -> int:
    _required_env_or_exit()
    server = ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    print(
        f"listener on :{PORT}  recipient={TRIAGE_RECIPIENT}  "
        f"runtime={urllib.parse.urlparse(RUNTIME_URL).hostname}",
        flush=True,
    )
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    return 0


if __name__ == "__main__":
    sys.exit(main())
