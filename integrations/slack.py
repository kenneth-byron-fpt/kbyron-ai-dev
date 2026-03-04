"""
Slack integration for Agent Farm

Required env vars:
  SLACK_BOT_TOKEN      xoxb-... bot token
  SLACK_CHANNEL_DOCS   channel for technical-writer output (e.g. #engineering-docs)
  SLACK_CHANNEL_MEETINGS channel for meeting summaries (e.g. #meeting-notes)
  SLACK_CHANNEL_AGENTS   channel for agent farm alerts (e.g. #ai-agents)
"""

import os
import httpx
from typing import Optional

SLACK_API = "https://slack.com/api"
BOT_TOKEN = os.environ.get("SLACK_BOT_TOKEN", "")

# Default channels — override with env vars
CHANNEL_DOCS     = os.environ.get("SLACK_CHANNEL_DOCS",     "#engineering-docs")
CHANNEL_MEETINGS = os.environ.get("SLACK_CHANNEL_MEETINGS", "#meeting-notes")
CHANNEL_AGENTS   = os.environ.get("SLACK_CHANNEL_AGENTS",   "#ai-agents")


def _headers() -> dict:
    return {
        "Authorization": f"Bearer {BOT_TOKEN}",
        "Content-Type": "application/json; charset=utf-8",
    }


def _check(resp: dict, context: str = ""):
    if not resp.get("ok"):
        raise RuntimeError(f"Slack API error{' (' + context + ')' if context else ''}: {resp.get('error')}")


# ---------------------------------------------------------------------------
# Core send
# ---------------------------------------------------------------------------

async def post_message(channel: str, text: str, blocks: Optional[list] = None) -> dict:
    """Post a plain or block-kit message to a channel."""
    payload: dict = {"channel": channel, "text": text}
    if blocks:
        payload["blocks"] = blocks

    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.post(f"{SLACK_API}/chat.postMessage", json=payload, headers=_headers())
        data = resp.json()

    _check(data, "post_message")
    return data


# ---------------------------------------------------------------------------
# Technical Writer notifications
# ---------------------------------------------------------------------------

async def notify_docs_generated(repo: str, pr_number: int, pr_title: str,
                                  docs_preview: str, comment_url: str = "") -> dict:
    """Notify #engineering-docs that the technical-writer generated docs for a PR."""
    preview = docs_preview[:400] + "…" if len(docs_preview) > 400 else docs_preview

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": "📝 Auto-Documentation Generated"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Repo:*\n{repo}"},
                {"type": "mrkdwn", "text": f"*PR:*\n<{comment_url}|#{pr_number}: {pr_title}>"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Preview:*\n```{preview}```"}},
        {"type": "divider"},
    ]

    fallback = f"📝 Docs generated for {repo} PR #{pr_number}: {pr_title}"
    return await post_message(CHANNEL_DOCS, fallback, blocks)


# ---------------------------------------------------------------------------
# Meeting Intelligence notifications
# ---------------------------------------------------------------------------

async def post_meeting_summary(meeting_title: str, summary: str,
                                 action_items: list[str], decisions: list[str],
                                 duration_min: int = 0) -> dict:
    """Post a meeting summary to #meeting-notes."""
    ai_text = "\n".join(f"• {a}" for a in action_items[:8]) or "_None detected_"
    dec_text = "\n".join(f"• {d}" for d in decisions[:5]) or "_None detected_"
    summary_short = summary[:600] + "…" if len(summary) > 600 else summary

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"🎯 Meeting Summary: {meeting_title}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": summary_short},
        },
        {"type": "divider"},
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*✅ Action Items:*\n{ai_text}"},
                {"type": "mrkdwn", "text": f"*🔑 Decisions:*\n{dec_text}"},
            ],
        },
    ]
    if duration_min:
        blocks.append({
            "type": "context",
            "elements": [{"type": "mrkdwn", "text": f"⏱ Duration: {duration_min} min"}],
        })

    fallback = f"🎯 Meeting summary: {meeting_title} — {len(action_items)} action items"
    return await post_message(CHANNEL_MEETINGS, fallback, blocks)


async def notify_action_item_routed(action: str, owner: str,
                                     due_date: str = "", jira_key: str = "") -> dict:
    """Notify that a meeting action item was routed."""
    jira_ref = f" → <https://jira.example.com/browse/{jira_key}|{jira_key}>" if jira_key else ""
    text = f"📋 Action item routed to *{owner}*: {action}{jira_ref}"
    if due_date:
        text += f" (due {due_date})"
    return await post_message(CHANNEL_MEETINGS, text)


# ---------------------------------------------------------------------------
# Daily / weekly digests
# ---------------------------------------------------------------------------

async def send_daily_digest(date: str, meetings_count: int, action_items_total: int,
                              auto_routed: int, highlights: list[str]) -> dict:
    """Post a daily agent farm digest to #ai-agents."""
    hi_text = "\n".join(f"• {h}" for h in highlights[:5]) or "_Quiet day_"

    blocks = [
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 Agent Farm Daily Digest — {date}"},
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Meetings:*\n{meetings_count}"},
                {"type": "mrkdwn", "text": f"*Action items:*\n{action_items_total}"},
                {"type": "mrkdwn", "text": f"*Auto-routed:*\n{auto_routed}"},
                {"type": "mrkdwn", "text": f"*Coverage:*\n{int(auto_routed/max(action_items_total,1)*100)}%"},
            ],
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": f"*Highlights:*\n{hi_text}"}},
    ]

    fallback = f"📊 Daily digest {date}: {meetings_count} meetings, {action_items_total} action items"
    return await post_message(CHANNEL_AGENTS, fallback, blocks)


async def notify_agent_error(agent_name: str, error: str, context: str = "") -> dict:
    """Alert #ai-agents that an agent encountered an error."""
    text = f"⚠️ *{agent_name}* error: {error}"
    if context:
        text += f"\n_{context}_"
    return await post_message(CHANNEL_AGENTS, text)
