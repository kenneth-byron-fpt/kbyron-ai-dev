"""
Meeting Intelligence Processor

Takes a raw transcript (from Recall.ai, Fireflies, or any source),
runs it through Claude (meeting-intelligence agent), then routes
the output to Slack and Jira.

Usage:
    from integrations.meeting_processor import process_meeting
    await process_meeting(transcript_data)

Transcript data format (Recall.ai compatible):
    {
        "meeting_id": "abc123",
        "title": "Sprint Planning",
        "platform": "zoom",         # or "teams"
        "started_at": "2026-03-03T09:00:00Z",
        "ended_at":   "2026-03-03T10:00:00Z",
        "participants": ["Alice", "Bob", "Kenneth"],
        "transcript": [
            {"speaker": "Alice", "text": "Let's discuss the roadmap.", "timestamp": 5.2},
            ...
        ]
    }
"""

import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic

from . import slack, jira

logger = logging.getLogger(__name__)

AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(Path.home() / ".claude" / "agents")))
LOG_DIR = Path(os.environ.get("LOG_DIR", str(Path(__file__).parent.parent / "webhook" / "logs")))
LOG_DIR.mkdir(parents=True, exist_ok=True)

MAX_TRANSCRIPT_CHARS = 80_000  # ~20K tokens worth of transcript


def _load_system_prompt(agent_name: str) -> str:
    path = AGENT_DIR / f"{agent_name}.md"
    content = path.read_text()
    if content.startswith("---"):
        end = content.index("---", 3)
        content = content[end + 3:].strip()
    return content


MEETING_AGENT_PROMPT = _load_system_prompt("meeting-intelligence")


def _format_transcript(transcript: list[dict]) -> str:
    """Convert transcript list to readable text."""
    lines = []
    for entry in transcript:
        ts = entry.get("timestamp", 0)
        mins, secs = divmod(int(ts), 60)
        speaker = entry.get("speaker", "Unknown")
        text = entry.get("text", "").strip()
        if text:
            lines.append(f"[{mins:02d}:{secs:02d}] {speaker}: {text}")
    return "\n".join(lines)


def _build_prompt(data: dict) -> str:
    title = data.get("title", "Untitled Meeting")
    platform = data.get("platform", "unknown").title()
    participants = ", ".join(data.get("participants", []))
    started = data.get("started_at", "")
    ended = data.get("ended_at", "")

    # Calculate duration
    duration_str = ""
    if started and ended:
        try:
            fmt = "%Y-%m-%dT%H:%M:%SZ"
            duration_min = int((datetime.strptime(ended, fmt) - datetime.strptime(started, fmt)).seconds / 60)
            duration_str = f"{duration_min} minutes"
        except Exception:
            pass

    raw_transcript = _format_transcript(data.get("transcript", []))
    if len(raw_transcript) > MAX_TRANSCRIPT_CHARS:
        raw_transcript = raw_transcript[:MAX_TRANSCRIPT_CHARS] + "\n\n... (transcript truncated)"

    return f"""\
Please analyze this meeting and produce a complete structured report.

## Meeting Metadata
- **Title:** {title}
- **Platform:** {platform}
- **Duration:** {duration_str or 'unknown'}
- **Participants:** {participants or 'unknown'}
- **Time:** {started}

## Full Transcript
{raw_transcript}

---

Please provide your analysis in the following JSON format (wrapped in ```json ... ```):

{{
  "summary": "2-4 sentence executive summary of meeting outcomes",
  "decisions": ["decision 1", "decision 2"],
  "action_items": [
    {{
      "task": "specific task description",
      "owner": "person name or 'unassigned'",
      "due_date": "YYYY-MM-DD or ''",
      "priority": "High|Medium|Low",
      "can_auto_route": true
    }}
  ],
  "key_topics": ["topic 1", "topic 2"],
  "risks": ["risk 1"],
  "recommendations": ["recommendation 1"],
  "meeting_effectiveness": "brief assessment"
}}

After the JSON, provide the full formatted meeting notes in markdown.
"""


async def _call_claude(prompt: str) -> str:
    """Stream meeting analysis from Claude."""
    client = anthropic.AsyncAnthropic()
    parts: list[str] = []

    async with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=8192,
        thinking={"type": "adaptive"},
        system=MEETING_AGENT_PROMPT,
        messages=[{"role": "user", "content": prompt}],
    ) as stream:
        async for text in stream.text_stream:
            parts.append(text)
        final = await stream.get_final_message()

    logger.info(
        "Meeting analysis — input: %d tokens, output: %d tokens",
        final.usage.input_tokens,
        final.usage.output_tokens,
    )
    return "".join(parts)


def _extract_json(response: str) -> dict:
    """Pull the JSON block out of Claude's response."""
    match = re.search(r"```json\s*(.*?)\s*```", response, re.DOTALL)
    if match:
        try:
            return json.loads(match.group(1))
        except json.JSONDecodeError as e:
            logger.warning("JSON parse error: %s", e)
    return {}


# ---------------------------------------------------------------------------
# Main entry point
# ---------------------------------------------------------------------------

async def process_meeting(data: dict) -> dict:
    """
    Full pipeline: transcript → Claude → Slack + Jira.
    Returns a summary dict of what was created.
    """
    title = data.get("title", "Untitled Meeting")
    meeting_id = data.get("meeting_id", "unknown")

    logger.info("Processing meeting: '%s' (%s)", title, meeting_id)

    # 1. Run transcript through Claude
    prompt = _build_prompt(data)
    response = await _call_claude(prompt)

    # 2. Parse structured output
    parsed = _extract_json(response)
    summary   = parsed.get("summary", "")
    decisions = parsed.get("decisions", [])
    action_items = parsed.get("action_items", [])
    recommendations = parsed.get("recommendations", [])

    # 3. Save full notes to disk
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_title = re.sub(r"[^a-zA-Z0-9_-]", "_", title)[:40]
    log_file = LOG_DIR / f"meeting_{safe_title}_{timestamp}.md"
    log_file.write_text(f"# Meeting Notes: {title}\n\n{response}\n")
    logger.info("Saved meeting notes to %s", log_file)

    # 4. Post summary to Slack
    slack_result = None
    try:
        slack_result = await slack.post_meeting_summary(
            meeting_title=title,
            summary=summary or response[:500],
            action_items=[a["task"] for a in action_items],
            decisions=decisions,
            duration_min=_get_duration(data),
        )
        logger.info("Posted meeting summary to Slack")
    except Exception as e:
        logger.warning("Slack post failed: %s", e)

    # 5. Create Jira tickets for high-priority action items
    jira_keys = []
    for item in action_items:
        if not item.get("can_auto_route", True):
            continue
        priority = item.get("priority", "Medium")
        if priority not in ("High", "Medium"):
            continue  # Skip Low priority items for now

        try:
            issue = await jira.create_from_action_item(
                action=item["task"],
                meeting_title=title,
                owner_name=item.get("owner", ""),
                due_date=item.get("due_date", ""),
                priority=priority,
            )
            jira_keys.append(issue["key"])
            logger.info("Created Jira ticket %s: %s", issue["key"], item["task"])

            # Notify Slack about each routed item
            try:
                await slack.notify_action_item_routed(
                    action=item["task"],
                    owner=item.get("owner", "unassigned"),
                    due_date=item.get("due_date", ""),
                    jira_key=issue["key"],
                )
            except Exception:
                pass

        except Exception as e:
            logger.warning("Jira ticket creation failed for '%s': %s", item["task"][:50], e)

    result = {
        "meeting_id": meeting_id,
        "title": title,
        "notes_file": str(log_file),
        "action_items_total": len(action_items),
        "jira_tickets_created": jira_keys,
        "slack_posted": slack_result is not None,
    }

    logger.info("Meeting processed: %d action items, %d Jira tickets", len(action_items), len(jira_keys))
    return result


def _get_duration(data: dict) -> int:
    try:
        fmt = "%Y-%m-%dT%H:%M:%SZ"
        return int(
            (datetime.strptime(data["ended_at"], fmt) - datetime.strptime(data["started_at"], fmt)).seconds / 60
        )
    except Exception:
        return 0
