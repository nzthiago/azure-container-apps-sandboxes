"""End-to-end OpenAI computer-use demo, driving an ACA sandbox.

Uses the OpenAI Agents SDK (``openai-agents``) with the ``ComputerTool``
backed by our ``ACAAsyncComputer`` adapter, talking to Azure OpenAI for
the model (``computer-use-preview`` deployment).

Flow:
  1. Boot a fresh `ubuntu` sandbox.
  2. Upload desktop-image/ and run setup.sh -- brings up Xvfb, Chrome,
     noVNC, and the FastAPI control server.
  3. Expose port 7000 (control) and 6080 (noVNC) publicly.
  4. Lock egress down: nothing in the sandbox can talk to the internet.
     The agent does all its work against `localhost:8080` inside the box.
  5. Build an Agent[ComputerTool[ACAAsyncComputer]] pointed at Azure OpenAI
     and let it complete an expense-report form.
  6. Verify the form's submission JSON.
  7. Delete the sandbox.

Required env (in samples/.env):
  ACA_SANDBOXGROUP_REGION, AZURE_SUBSCRIPTION_ID, ACA_RESOURCE_GROUP,
  ACA_SANDBOX_GROUP, AZURE_OPENAI_ENDPOINT, AZURE_OPENAI_API_KEY,
  AZURE_OPENAI_COMPUTER_USE_DEPLOYMENT (deployment name for
  computer-use-preview on your AOAI resource).

Optional:
  AZURE_OPENAI_API_VERSION (default: 2025-04-01-preview)

Flags:
  --manual    Skip the agent loop. Boot the desktop, print the noVNC URL,
              and wait for Enter. Use this to demo the platform without
              an LLM, or to drive the form yourself.
"""

from __future__ import annotations

import argparse
import asyncio
import os
import sys
import uuid
from pathlib import Path

# Tracing in openai-agents defaults to the OpenAI cloud trace exporter,
# which requires an OPENAI_API_KEY. We're on Azure, so disable it before
# any agents import to avoid the noisy "skipping trace export" warnings.
os.environ.setdefault("OPENAI_AGENTS_DISABLE_TRACING", "1")

from azure.identity import DefaultAzureCredential
from azure.containerapps.sandbox import (
    SandboxGroupClient,
    endpoint_for_region,
)
from openai import AsyncAzureOpenAI
from agents import Agent, ComputerTool, ModelSettings, Runner
from agents import set_tracing_disabled
from agents.exceptions import MaxTurnsExceeded
from agents.models.openai_responses import OpenAIResponsesModel

set_tracing_disabled(True)

from aca_computer import ACAAsyncComputer

DEFAULT_API_VERSION = "2025-04-01-preview"
DISPLAY_W = 1280
DISPLAY_H = 800
CONTROL_PORT = 7000
NOVNC_PORT = 6080
MAX_AGENT_TURNS = 80

DESKTOP_DIR = Path(__file__).resolve().parents[2] / "desktop-image"

TASK_PROMPT = """\
You are an operator driving a Linux desktop. A browser is already open
to an expense-report form at http://localhost:8080/. Fill the form out
exactly as specified below and then click the "Submit expense report"
button.

Trip details
  Trip name:        AI Apps customer workshop
  Start date:       2025-05-15
  End date:         2025-05-17
  Business purpose: On-site customer workshop for the AI Apps team

Line items (one row is pre-populated; click "Add line item" for each
additional row). The form's Category dropdown choices are exactly:
Airfare, Hotel, Meals, Ground transport, Other.
  1. Category Hotel             Description "Hotel night 1"   Amount 312.40
  2. Category Hotel             Description "Hotel night 2"   Amount 312.40
  3. Category Meals             Description "Welcome dinner"  Amount  42.10
  4. Category Ground transport  Description "Taxi to venue"   Amount  28.75

When the form is submitted you'll see a green "Submitted." confirmation
below the form. Stop then. Do not navigate away. Use screenshots between
actions to verify progress.
"""


def _load_env() -> None:
    for parent in Path(__file__).resolve().parents:
        env = parent / ".env"
        if env.is_file():
            for line in env.read_text().splitlines():
                if line and not line.startswith("#") and "=" in line:
                    k, v = line.split("=", 1)
                    os.environ.setdefault(k.strip(), v.strip())
            break


def _require(name: str) -> str:
    v = os.environ.get(name)
    if not v:
        sys.exit(f"error: missing env var {name}\n       set it in samples/.env")
    return v


def _upload_desktop_image(sandbox) -> None:
    print(f"==> Uploading desktop image from {DESKTOP_DIR}...")
    sandbox.exec("mkdir -p /opt/desktop/form")
    for src in DESKTOP_DIR.rglob("*"):
        if src.is_dir():
            continue
        rel = src.relative_to(DESKTOP_DIR).as_posix()
        dest = f"/opt/desktop/{rel}"
        data = src.read_bytes()
        # Strip CRLF -> LF (we run on Windows; targets are Linux).
        data = data.replace(b"\r\n", b"\n")
        sandbox.write_file(dest, data)
    sandbox.exec("chmod +x /opt/desktop/setup.sh")


def _install_desktop(sandbox) -> None:
    print("==> Running setup.sh (~2-4 min: apt installs Chrome + noVNC + ...)...")
    r = sandbox.exec("bash /opt/desktop/setup.sh", timeout=600)
    if r.stdout:
        sys.stdout.write(r.stdout)
        if not r.stdout.endswith("\n"):
            sys.stdout.write("\n")
    if r.stderr:
        sys.stderr.write(r.stderr)
        if not r.stderr.endswith("\n"):
            sys.stderr.write("\n")
    if r.exit_code != 0:
        raise RuntimeError(f"setup.sh failed: exit={r.exit_code}")


def _expose(sandbox, port: int, label: str) -> str:
    p = sandbox.add_port(port, anonymous=True)
    url = getattr(p, "url", None)
    if not url:
        raise RuntimeError(f"add_port({port}) returned no URL")
    print(f"    {label:<7} : {url}")
    return url


def _build_agent(computer: ACAAsyncComputer, deployment: str) -> Agent:
    aoai = AsyncAzureOpenAI(
        api_key=_require("AZURE_OPENAI_API_KEY"),
        azure_endpoint=_require("AZURE_OPENAI_ENDPOINT"),
        api_version=os.environ.get("AZURE_OPENAI_API_VERSION", DEFAULT_API_VERSION),
    )
    # For Azure, the OpenAIResponsesModel `model` string is the deployment
    # name, not the OpenAI model id.
    model = OpenAIResponsesModel(model=deployment, openai_client=aoai)
    return Agent(
        name="ACA computer operator",
        instructions=(
            "You operate a Linux desktop via the computer tool. Take a "
            "screenshot first to ground yourself. Click precisely, verify "
            "with screenshots between actions, and stop as soon as the "
            "task is complete."
        ),
        tools=[ComputerTool(computer=computer)],
        model=model,
        model_settings=ModelSettings(truncation="auto"),
    )


async def _run_agent(control_url: str, deployment: str, prompt: str) -> int:
    async with ACAAsyncComputer(
        control_url, width=DISPLAY_W, height=DISPLAY_H
    ) as computer:
        agent = _build_agent(computer, deployment)
        print(f"==> Running agent (cap: {MAX_AGENT_TURNS} turns)...")
        try:
            result = await Runner.run(
                agent,
                input=prompt,
                max_turns=MAX_AGENT_TURNS,
            )
            print()
            print("agent final output:")
            print(result.final_output)
        except MaxTurnsExceeded:
            print()
            print(
                f"agent hit the {MAX_AGENT_TURNS}-turn cap without finishing. "
                "Raise MAX_AGENT_TURNS in computer_use.py or shorten the task."
            )

        print()
        submission = await computer.fetch_submission()
        if submission:
            print("form submission captured:")
            for k, v in submission.items():
                print(f"  {k}: {v}")
        else:
            print("(no /tmp/submission.json yet)")
        return 0


def _manual_wait(control_url: str, novnc_url: str) -> None:
    print()
    print("=" * 76)
    print("  --manual: no AI loop. Open this URL in your browser:")
    print()
    print(f"    {novnc_url}/vnc.html?autoconnect=1&resize=remote")
    print()
    print("  Or drive the control server from another terminal:")
    print(f"    curl -X POST {control_url}/click \\")
    print('      -H "Content-Type: application/json" -d \'{"x":300,"y":250}\'')
    print(f"    curl {control_url}/submission")
    print("=" * 76)
    print()
    try:
        input("Press Enter to delete the sandbox when you're done... ")
    except (EOFError, KeyboardInterrupt):
        print()


async def _amain(args: argparse.Namespace) -> int:
    _load_env()
    deployment = None
    if not args.manual:
        deployment = _require("AZURE_OPENAI_COMPUTER_USE_DEPLOYMENT")
    run_id = uuid.uuid4().hex[:8]
    credential = DefaultAzureCredential()
    client = SandboxGroupClient(
        endpoint_for_region(_require("ACA_SANDBOXGROUP_REGION")),
        credential,
        subscription_id=_require("AZURE_SUBSCRIPTION_ID"),
        resource_group=_require("ACA_RESOURCE_GROUP"),
        sandbox_group=_require("ACA_SANDBOX_GROUP"),
    )

    sandbox = None
    rc = 1
    try:
        print(f"==> Booting sandbox (run={run_id})...")
        sandbox = client.begin_create_sandbox(
            disk="ubuntu",
            cpu="2000m",
            memory="4096Mi",
            labels={"scenario": "computer-use", "run": run_id},
        ).result()
        print(f"    sandbox: {sandbox.sandbox_id}")

        _upload_desktop_image(sandbox)
        # If the user passed --start-url, retarget Chrome before setup boots it.
        # Write the URL to a file setup.sh reads, so we don't have to interpolate
        # arbitrary user input into a shell command.
        if args.start_url:
            sandbox.write_file(
                "/opt/desktop/start_url.txt",
                args.start_url.encode("utf-8"),
            )
        _install_desktop(sandbox)

        print("==> Exposing ports...")
        control_url = _expose(sandbox, CONTROL_PORT, "control")
        novnc_url = _expose(sandbox, NOVNC_PORT, "noVNC")

        if args.allow_internet:
            print("==> Leaving sandbox egress open (--allow-internet).")
        else:
            # Lock egress down AFTER setup ran (apt needed internet). The agent
            # talks only to localhost:8080 inside the sandbox; the operator
            # process here talks to control_url via ACA ingress, independent of
            # the sandbox's egress.
            print("==> Locking sandbox egress (deny-by-default)...")
            sandbox.set_egress_default("Deny")

        if args.manual:
            _manual_wait(control_url, novnc_url)
            rc = 0
        else:
            spectate = f"{novnc_url}/vnc.html?autoconnect=1&resize=remote"
            prompt = args.prompt or TASK_PROMPT
            print()
            print("=" * 76)
            print("  Open this URL in your browser to watch the agent live:")
            print()
            print(f"    {spectate}")
            print()
            print("  Task the agent will perform:")
            print()
            first = prompt.strip().splitlines()[0]
            print(f"    {first}{'...' if len(prompt.strip()) > len(first) else ''}")
            print()
            print("  Press Enter to launch the agent.")
            print("=" * 76)
            try:
                input("> ")
            except (EOFError, KeyboardInterrupt):
                print()
            rc = await _run_agent(control_url, deployment, prompt)
    finally:
        if sandbox is not None:
            print(f"==> Deleting sandbox {sandbox.sandbox_id}...")
            try:
                sandbox.delete()
            except Exception as e:  # noqa: BLE001
                print(f"    warning: delete failed: {e}")
        client.close()
        credential.close()
    return rc


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--manual",
        action="store_true",
        help="skip the agent loop; bring up the desktop and wait for Enter",
    )
    parser.add_argument(
        "--prompt",
        "-p",
        type=str,
        default=None,
        help=(
            "task to give the agent. Defaults to the built-in expense-report "
            "form demo. Pass any instruction here, e.g. "
            '-p "go to https://news.ycombinator.com and tell me the top story"'
        ),
    )
    parser.add_argument(
        "--start-url",
        type=str,
        default=None,
        help=(
            "URL to point Chrome at before the agent starts. Defaults to the "
            "in-sandbox demo form (http://localhost:8080/). Useful with "
            "--prompt to point at a real site."
        ),
    )
    parser.add_argument(
        "--allow-internet",
        action="store_true",
        help=(
            "Skip the egress lockdown so the sandbox can reach the public "
            "internet. Required when --start-url points to an external site."
        ),
    )
    args = parser.parse_args()
    return asyncio.run(_amain(args))


if __name__ == "__main__":
    sys.exit(main())
