"""
Meeting Prep Bot — Prepares briefing notes before your upcoming meetings.

Calls connectors directly via invoke_dynamic (no MCP server needed):
  1. Fetches upcoming calendar events (Office 365)
  2. Searches emails for related threads (Office 365)
  3. Generates a briefing note and saves to OneDrive
  4. Emails you the summary before the meeting

Usage:
  1. Set up connection first (see SKILL.md Step 1-2)
  2. Run: python mcp_app.py --resource-group my-rg --gateway my-gw --connection my-o365

Required connector operations:
  - Office 365: GetEvents, GetEmails, SendMailV2
  - OneDrive: CreateFile
"""

import argparse
import json
import os
import sys
from datetime import datetime, timedelta, timezone

from connector import ConnectorClient  # type: ignore[import-untyped]

parser = argparse.ArgumentParser(description="Meeting Prep Bot")
parser.add_argument("-g", "--resource-group", required=True)
parser.add_argument("--gateway", required=True)
parser.add_argument("--connection", required=True, help="Office 365 connection name")
parser.add_argument("--onedrive-connection", default=None, help="OneDrive connection (defaults to same)")
parser.add_argument("--email", default=None, help="Your email for receiving briefings")
args = parser.parse_args()

client = ConnectorClient(resource_group=args.resource_group)
gw = args.gateway
conn = args.connection
od_conn = args.onedrive_connection or conn


def get_upcoming_events(hours_ahead: int = 24) -> list:
    """Fetch calendar events via Office 365 connector."""
    now = datetime.now(timezone.utc)
    end = now + timedelta(hours=hours_ahead)
    result = client.invoke_dynamic(gw, conn,
        operation_id="GetEvents",
        parameters={
            "$filter": f"start/dateTime ge '{now.isoformat()}' and start/dateTime le '{end.isoformat()}'",
            "$top": 10,
            "$orderby": "start/dateTime",
        })
    return result.get("value", [])


def search_related_emails(subject: str) -> list:
    """Search inbox for emails related to a meeting topic."""
    result = client.invoke_dynamic(gw, conn,
        operation_id="GetEmails",
        parameters={
            "folderPath": "Inbox",
            "$filter": f"contains(subject, '{subject}')",
            "$top": 5,
        })
    return result.get("value", [])


def generate_briefing(event: dict, emails: list) -> str:
    """Compose a briefing note from gathered context."""
    subject = event.get("subject", "Unknown Meeting")
    start = event.get("start", {}).get("dateTime", "")
    attendees = [a.get("emailAddress", {}).get("address", "")
                 for a in event.get("attendees", [])]

    briefing = []
    briefing.append(f"# Meeting Briefing: {subject}")
    briefing.append(f"**When:** {start}")
    briefing.append(f"**Attendees:** {', '.join(attendees)}")
    briefing.append("")

    if emails:
        briefing.append("## Recent Email Context")
        for email in emails[:5]:
            subj = email.get("subject", "No subject")
            sender = email.get("from", {}).get("emailAddress", {}).get("address", "")
            briefing.append(f"- **{subj}** (from {sender})")
        briefing.append("")
    else:
        briefing.append("_No related emails found._\n")

    return "\n".join(briefing)


def save_to_onedrive(briefing: str, meeting_subject: str) -> str:
    """Save briefing note to OneDrive /MeetingPrep/ folder."""
    date_str = datetime.now().strftime("%Y-%m-%d")
    safe_name = "".join(c if c.isalnum() or c in " -_" else "" for c in meeting_subject)
    filename = f"{date_str}-{safe_name[:40]}.md"

    client.invoke_dynamic(gw, od_conn,
        operation_id="CreateFile",
        parameters={
            "folderPath": "/MeetingPrep",
            "name": filename,
            "body": briefing,
        })
    return f"/MeetingPrep/{filename}"


def email_briefing(briefing: str, meeting_subject: str, recipient: str):
    """Email the briefing summary via Office 365."""
    client.invoke_dynamic(gw, conn,
        operation_id="SendMailV2",
        parameters={
            "emailMessage": {
                "To": recipient,
                "Subject": f"[Meeting Prep] {meeting_subject}",
                "Body": f"<pre>{briefing}</pre>",
                "Importance": "Normal",
            }
        })


def main():
    print("=" * 60)
    print("  Meeting Prep Bot — Direct Connector Invocation")
    print("=" * 60)
    print(f"  Gateway: {gw}")
    print(f"  Connection: {conn}")

    my_email = args.email or os.environ.get("USER_EMAIL", "me@contoso.com")
    print(f"\nFetching upcoming events...")

    events = get_upcoming_events(hours_ahead=24)
    if not events:
        print("No upcoming events in the next 24 hours. Nothing to prep!")
        return

    print(f"Found {len(events)} upcoming event(s).\n")

    for event in events:
        subject = event.get("subject", "Unknown")
        print(f"--- Preparing: {subject} ---")

        print("  Searching related emails...")
        emails = search_related_emails(subject)

        briefing = generate_briefing(event, emails)

        print("  Saving briefing to OneDrive...")
        path = save_to_onedrive(briefing, subject)
        print(f"  Saved: {path}")

        print(f"  Emailing briefing to {my_email}...")
        email_briefing(briefing, subject, my_email)

        print(f"  ✓ Done\n")

    print("All meetings prepped!")


if __name__ == "__main__":
    main()
