"""
Jira integration for Agent Farm

Required env vars:
  JIRA_URL          https://yourcompany.atlassian.net
  JIRA_EMAIL        your-email@company.com
  JIRA_API_TOKEN    API token from id.atlassian.com/manage-profile/security/api-tokens
  JIRA_PROJECT_KEY  Default project key (e.g. ENG, PROD, OPS)
"""

import base64
import os
from typing import Optional

import httpx

JIRA_URL      = os.environ.get("JIRA_URL", "").rstrip("/")
JIRA_EMAIL    = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN    = os.environ.get("JIRA_API_TOKEN", "")
DEFAULT_PROJECT = os.environ.get("JIRA_PROJECT_KEY", "ENG")


def _headers() -> dict:
    creds = base64.b64encode(f"{JIRA_EMAIL}:{JIRA_TOKEN}".encode()).decode()
    return {
        "Authorization": f"Basic {creds}",
        "Content-Type": "application/json",
        "Accept": "application/json",
    }


def _check(resp: httpx.Response, context: str = ""):
    if resp.status_code not in (200, 201):
        raise RuntimeError(
            f"Jira API error{' (' + context + ')' if context else ''}: "
            f"{resp.status_code} — {resp.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Core issue operations
# ---------------------------------------------------------------------------

async def create_issue(
    summary: str,
    description: str,
    issue_type: str = "Task",
    project_key: Optional[str] = None,
    assignee_account_id: Optional[str] = None,
    priority: Optional[str] = None,
    labels: Optional[list[str]] = None,
    due_date: Optional[str] = None,          # "2026-03-25"
) -> dict:
    """Create a Jira issue. Returns the full created issue dict (with key, id, url)."""
    fields: dict = {
        "project": {"key": project_key or DEFAULT_PROJECT},
        "summary": summary,
        "description": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": description}],
                }
            ],
        },
        "issuetype": {"name": issue_type},
    }

    # Only set priority if explicitly provided — avoids project-specific name mismatches
    if priority:
        fields["priority"] = {"name": priority}

    if assignee_account_id:
        fields["assignee"] = {"accountId": assignee_account_id}
    if labels:
        fields["labels"] = labels
    if due_date:
        fields["duedate"] = due_date

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{JIRA_URL}/rest/api/3/issue",
            json={"fields": fields},
            headers=_headers(),
        )
    _check(resp, "create_issue")
    data = resp.json()
    data["browse_url"] = f"{JIRA_URL}/browse/{data['key']}"
    return data


async def add_comment(issue_key: str, comment: str) -> dict:
    """Add a plain-text comment to a Jira issue."""
    body = {
        "body": {
            "type": "doc",
            "version": 1,
            "content": [
                {
                    "type": "paragraph",
                    "content": [{"type": "text", "text": comment}],
                }
            ],
        }
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{JIRA_URL}/rest/api/3/issue/{issue_key}/comment",
            json=body,
            headers=_headers(),
        )
    _check(resp, "add_comment")
    return resp.json()


async def get_issue(issue_key: str) -> dict:
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(
            f"{JIRA_URL}/rest/api/3/issue/{issue_key}",
            headers=_headers(),
        )
    _check(resp, "get_issue")
    return resp.json()


# ---------------------------------------------------------------------------
# Agent Farm helpers
# ---------------------------------------------------------------------------

async def create_from_action_item(
    action: str,
    meeting_title: str,
    owner_name: str = "",
    due_date: str = "",
    priority: Optional[str] = None,
    transcript_excerpt: str = "",
) -> dict:
    """Create a Jira task from a meeting action item."""
    desc_parts = [f"Action item from meeting: *{meeting_title}*"]
    if owner_name:
        desc_parts.append(f"Assigned to: {owner_name}")
    if transcript_excerpt:
        desc_parts.append(f"\nContext:\n{transcript_excerpt}")
    desc_parts.append("\n_Auto-created by Meeting Intelligence Agent_")

    return await create_issue(
        summary=action,
        description="\n".join(desc_parts),
        issue_type="Task",
        priority=priority,
        labels=["meeting-action-item", "agent-farm"],
        due_date=due_date or None,
    )


async def create_docs_ticket(
    pr_title: str,
    repo: str,
    pr_number: int,
    docs_summary: str,
) -> dict:
    """Create a Jira ticket to review auto-generated documentation."""
    return await create_issue(
        summary=f"Review auto-generated docs: {repo} PR #{pr_number}",
        description=(
            f"The Technical Writer Agent generated documentation for:\n"
            f"PR #{pr_number}: {pr_title} in {repo}\n\n"
            f"Preview:\n{docs_summary[:500]}\n\n"
            f"_Auto-created by Technical Writer Agent_"
        ),
        issue_type="Task",
        labels=["documentation", "agent-farm", "auto-generated"],
    )
