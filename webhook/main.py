#!/usr/bin/env python3
"""
GitHub Webhook Server — Technical Writer Agent

Listens for merged PRs, calls Claude (technical-writer) to generate
documentation, and posts results as a PR comment.
"""

import asyncio
import hashlib
import hmac
import json
import logging
import os
import re
from datetime import datetime
from pathlib import Path

import anthropic
import httpx
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse

# Agent Farm integrations (Slack, Jira)
import sys
sys.path.insert(0, str(Path(__file__).parent.parent))
from integrations import slack, jira

# ---------------------------------------------------------------------------
# Configuration
# ---------------------------------------------------------------------------

WEBHOOK_SECRET = os.environ["GITHUB_WEBHOOK_SECRET"].encode()
GITHUB_TOKEN = os.environ["GITHUB_TOKEN"]

AGENT_DIR = Path(os.environ.get("AGENT_DIR", str(Path.home() / ".claude" / "agents")))
LOG_DIR = Path(__file__).parent / "logs"
LOG_DIR.mkdir(parents=True, exist_ok=True)

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
)
logger = logging.getLogger(__name__)

app = FastAPI(title="Agent Farm Webhook Server — Technical Writer")


# ---------------------------------------------------------------------------
# Load technical-writer system prompt at startup
# ---------------------------------------------------------------------------

def _load_system_prompt(agent_name: str) -> str:
    path = AGENT_DIR / f"{agent_name}.md"
    content = path.read_text()
    # Strip YAML frontmatter (content between first pair of --- delimiters)
    if content.startswith("---"):
        second = content.index("---", 3)
        content = content[second + 3:].strip()
    return content


TECHNICAL_WRITER_PROMPT = _load_system_prompt("technical-writer")
logger.info("Loaded technical-writer system prompt (%d chars)", len(TECHNICAL_WRITER_PROMPT))


# ---------------------------------------------------------------------------
# HMAC signature verification
# ---------------------------------------------------------------------------

def verify_signature(payload: bytes, signature_header: str) -> bool:
    expected = "sha256=" + hmac.new(
        WEBHOOK_SECRET, payload, hashlib.sha256
    ).hexdigest()
    return hmac.compare_digest(expected, signature_header)


# ---------------------------------------------------------------------------
# GitHub API helpers
# ---------------------------------------------------------------------------

GITHUB_HEADERS = {
    "Authorization": f"Bearer {GITHUB_TOKEN}",
    "Accept": "application/vnd.github+json",
    "X-GitHub-Api-Version": "2022-11-28",
}

MAX_DIFF_CHARS = 60_000  # Truncate very large diffs to stay within context


async def get_pr_diff(owner: str, repo: str, pr_number: int) -> str:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}"
    diff_headers = {**GITHUB_HEADERS, "Accept": "application/vnd.github.diff"}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=diff_headers)
        resp.raise_for_status()
        diff = resp.text
    if len(diff) > MAX_DIFF_CHARS:
        diff = diff[:MAX_DIFF_CHARS] + "\n\n... (diff truncated)"
    return diff


async def get_pr_files(owner: str, repo: str, pr_number: int) -> list[dict]:
    url = f"https://api.github.com/repos/{owner}/{repo}/pulls/{pr_number}/files"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(url, headers=GITHUB_HEADERS)
        resp.raise_for_status()
        return resp.json()


async def post_pr_comment(owner: str, repo: str, pr_number: int, body: str) -> dict:
    url = f"https://api.github.com/repos/{owner}/{repo}/issues/{pr_number}/comments"
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(url, json={"body": body}, headers=GITHUB_HEADERS)
        resp.raise_for_status()
        return resp.json()


# ---------------------------------------------------------------------------
# Claude — documentation generation
# ---------------------------------------------------------------------------

def _build_user_message(ctx: dict) -> str:
    file_lines = "\n".join(
        f"- `{f['filename']}` ({f['status']}, +{f['additions']}/-{f['deletions']})"
        for f in ctx["files"]
    )
    return f"""\
A pull request has just been merged. Please generate documentation for these changes.

## PR Details
- **Repository:** {ctx['repo']}
- **PR #{ctx['number']}:** {ctx['title']}
- **Author:** {ctx['author']}
- **Branch:** `{ctx['head_branch']}` → `{ctx['base_branch']}`

## PR Description
{ctx['body'] or '*(no description provided)*'}

## Changed Files ({len(ctx['files'])} files)
{file_lines}

## Diff
```diff
{ctx['diff']}
```

Based on these changes, please:
1. Identify what needs documentation (new/changed APIs, functions, config, dependencies, breaking changes)
2. Generate the appropriate documentation (API reference, changelog entry, README updates, migration guide if needed)
3. Format each doc as a ready-to-commit markdown file, prefixed with its intended file path

Focus on what's most valuable to developers consuming this code.
"""


async def generate_docs(pr_context: dict) -> str:
    client = anthropic.AsyncAnthropic()
    docs_parts: list[str] = []

    async with client.messages.stream(
        model="claude-opus-4-6",
        max_tokens=8192,
        thinking={"type": "adaptive"},
        system=TECHNICAL_WRITER_PROMPT,
        messages=[{"role": "user", "content": _build_user_message(pr_context)}],
    ) as stream:
        async for text in stream.text_stream:
            docs_parts.append(text)
        final = await stream.get_final_message()

    logger.info(
        "Claude usage — input: %d tokens, output: %d tokens",
        final.usage.input_tokens,
        final.usage.output_tokens,
    )
    return "".join(docs_parts)


# ---------------------------------------------------------------------------
# Webhook endpoint
# ---------------------------------------------------------------------------

@app.post("/webhook")
async def handle_webhook(request: Request):
    body = await request.body()

    # Verify GitHub signature
    sig = request.headers.get("X-Hub-Signature-256", "")
    if not sig or not verify_signature(body, sig):
        logger.warning("Invalid or missing webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    event_type = request.headers.get("X-GitHub-Event", "")
    payload = json.loads(body)

    logger.info("Received GitHub event: %s", event_type)

    # Only care about merged pull requests
    if event_type != "pull_request":
        return JSONResponse({"status": "ignored", "reason": f"event={event_type}"})

    action = payload.get("action")
    merged = payload.get("pull_request", {}).get("merged", False)

    if action != "closed" or not merged:
        return JSONResponse({"status": "ignored", "reason": "not a merged PR"})

    pr = payload["pull_request"]
    repo_info = payload["repository"]
    owner = repo_info["owner"]["login"]
    repo = repo_info["name"]
    pr_number = pr["number"]

    logger.info("Processing merged PR #%d: '%s' in %s/%s", pr_number, pr["title"], owner, repo)

    # Fetch PR data from GitHub
    try:
        diff, files = await asyncio.gather(
            get_pr_diff(owner, repo, pr_number),
            get_pr_files(owner, repo, pr_number),
        )
    except httpx.HTTPStatusError as e:
        logger.error("GitHub API error: %s", e)
        raise HTTPException(status_code=502, detail=f"GitHub API error: {e.response.status_code}")
    except httpx.RequestError as e:
        logger.error("GitHub connection error: %s", e)
        raise HTTPException(status_code=502, detail="GitHub connection error")

    pr_context = {
        "repo": f"{owner}/{repo}",
        "number": pr_number,
        "title": pr["title"],
        "author": pr["user"]["login"],
        "head_branch": pr["head"]["ref"],
        "base_branch": pr["base"]["ref"],
        "body": pr.get("body") or "",
        "files": files[:25],  # Cap at 25 files
        "diff": diff,
    }

    # Generate documentation via Claude
    try:
        docs = await generate_docs(pr_context)
    except anthropic.APIError as e:
        logger.error("Claude API error: %s", e)
        raise HTTPException(status_code=502, detail=f"Claude API error: {e}")

    # Save docs to log file
    timestamp = datetime.utcnow().strftime("%Y%m%d_%H%M%S")
    safe_repo = re.sub(r"[^a-zA-Z0-9_-]", "_", repo)
    log_file = LOG_DIR / f"docs_{owner}_{safe_repo}_pr{pr_number}_{timestamp}.md"
    log_file.write_text(
        f"# Auto-generated docs: {owner}/{repo} PR #{pr_number}\n\n{docs}\n"
    )
    logger.info("Saved docs to %s", log_file)

    # Post as PR comment
    comment = (
        f"## 📝 Auto-generated Documentation\n\n"
        f"{docs}\n\n"
        f"---\n"
        f"*Generated by Technical Writer Agent on {datetime.utcnow().strftime('%Y-%m-%d %H:%M UTC')}*"
    )
    comment_url = ""
    try:
        comment_data = await post_pr_comment(owner, repo, pr_number, comment)
        comment_url = comment_data.get("html_url", "")
        logger.info("Posted comment %s on PR #%d", comment_data.get("id"), pr_number)
        posted = True
    except httpx.HTTPError as e:
        logger.warning("Failed to post PR comment: %s", e)
        posted = False

    # Notify Slack
    try:
        await slack.notify_docs_generated(
            repo=f"{owner}/{repo}",
            pr_number=pr_number,
            pr_title=pr["title"],
            docs_preview=docs,
            comment_url=comment_url,
        )
        logger.info("Slack notification sent")
    except Exception as e:
        logger.warning("Slack notification failed: %s", e)

    # Create Jira ticket for doc review (optional — only if JIRA_URL is set)
    jira_key = ""
    if os.environ.get("JIRA_URL"):
        try:
            issue = await jira.create_docs_ticket(
                pr_title=pr["title"],
                repo=f"{owner}/{repo}",
                pr_number=pr_number,
                docs_summary=docs[:400],
            )
            jira_key = issue["key"]
            logger.info("Created Jira ticket %s for doc review", jira_key)
        except Exception as e:
            logger.warning("Jira ticket creation failed: %s", e)

    return JSONResponse({
        "status": "processed",
        "pr": f"{owner}/{repo}#{pr_number}",
        "comment_posted": posted,
        "slack_notified": True,
        "jira_ticket": jira_key or None,
        "docs_file": str(log_file),
    })


@app.post("/meeting")
async def handle_meeting(request: Request):
    """
    Recall.ai webhook endpoint.

    Handles two event types:
      bot.status_change (code="done") — meeting ended, fetch + process transcript
      transcript.data                 — real-time utterance (logged, not processed yet)

    Signature verification uses Recall.ai's Svix-compatible HMAC-SHA256.
    Configure RECALL_WEBHOOK_SECRET (whsec_...) to enable it.
    """
    from integrations import recall
    from integrations.meeting_processor import process_meeting

    body = await request.body()

    # Verify Recall.ai webhook signature
    if not recall.verify_webhook_signature(body, dict(request.headers)):
        logger.warning("Invalid Recall.ai webhook signature")
        raise HTTPException(status_code=401, detail="Invalid signature")

    payload = json.loads(body)
    event = recall.parse_webhook(payload)

    if not event:
        return JSONResponse({"status": "ignored"})

    event_type = event.get("type", "")
    bot_id     = event.get("bot_id", "")

    logger.info("Recall.ai event: %s  bot: %s", event_type, bot_id)

    # Real-time transcript chunk — log and acknowledge
    if event_type == "transcript.data":
        return JSONResponse({"status": "ack"})

    # Meeting ended — fetch full transcript and process
    if event_type == "bot.done":
        if not bot_id:
            raise HTTPException(status_code=400, detail="Missing bot_id")

        # Fetch bot metadata to get meeting info
        try:
            bot_data = await recall.get_bot(bot_id)
        except Exception as e:
            logger.error("Failed to fetch bot %s: %s", bot_id, e)
            raise HTTPException(status_code=502, detail="Recall.ai API error")

        # Download transcript
        try:
            raw_segments = await recall.download_transcript(bot_id)
        except Exception as e:
            logger.error("Failed to download transcript for bot %s: %s", bot_id, e)
            raise HTTPException(status_code=502, detail="Transcript download error")

        transcript = recall.normalize_transcript(raw_segments)

        if not transcript:
            logger.warning("Bot %s returned empty transcript", bot_id)
            return JSONResponse({"status": "empty_transcript", "bot_id": bot_id})

        # Build meeting data for the processor
        participants = [
            p.get("name", "Unknown")
            for p in bot_data.get("meeting_participants", [])
        ]

        meeting_data = {
            "meeting_id": bot_id,
            "title": bot_data.get("bot_name", "Untitled Meeting"),
            "platform": _detect_platform(bot_data.get("meeting_url", "")),
            "started_at": bot_data.get("started_at", ""),
            "ended_at": bot_data.get("ended_at", ""),
            "participants": participants,
            "transcript": transcript,
        }

        try:
            result = await process_meeting(meeting_data)
        except Exception as e:
            logger.error("Meeting processing error for bot %s: %s", bot_id, e)
            raise HTTPException(status_code=500, detail=str(e))

        return JSONResponse(result)

    # Any other status change — acknowledge and ignore
    return JSONResponse({"status": "ignored", "event": event_type})


@app.post("/meeting/join")
async def join_meeting(request: Request):
    """
    Manually dispatch a Recall.ai bot to a meeting.

    Body: {"url": "https://zoom.us/j/...", "title": "optional title"}

    The bot joins immediately. When the meeting ends, Recall.ai posts to /meeting.
    Configure your Recall.ai webhook in the dashboard to point here.
    """
    from integrations import recall

    data = await request.json()
    url = data.get("url", "").strip()
    if not url:
        raise HTTPException(status_code=400, detail="Missing 'url' field")

    if not recall.RECALL_API_KEY:
        raise HTTPException(status_code=503, detail="RECALL_API_KEY not configured")

    title = data.get("title", "")
    bot_name = f"Meeting Intelligence{' — ' + title if title else ''}"

    try:
        bot = await recall.create_bot(meeting_url=url, bot_name=bot_name[:100])
    except Exception as e:
        logger.error("Failed to create Recall.ai bot: %s", e)
        raise HTTPException(status_code=502, detail=str(e))

    logger.info("Bot %s dispatched to: %s", bot["id"], url)
    return JSONResponse({
        "status": "bot_dispatched",
        "bot_id": bot["id"],
        "meeting_url": url,
    })


def _detect_platform(meeting_url: str) -> str:
    if "zoom.us" in meeting_url:
        return "zoom"
    if "teams.microsoft.com" in meeting_url or "teams.live.com" in meeting_url:
        return "teams"
    if "meet.google.com" in meeting_url:
        return "google_meet"
    if "webex.com" in meeting_url:
        return "webex"
    return "unknown"


@app.get("/health")
async def health():
    from integrations import recall
    return {
        "status": "ok",
        "endpoints": {
            "github_webhook":    "POST /webhook",
            "recall_webhook":    "POST /meeting",
            "manual_bot_join":   "POST /meeting/join",
        },
        "agents_loaded": {
            "technical-writer":      bool(TECHNICAL_WRITER_PROMPT),
            "meeting-intelligence":  True,
        },
        "integrations": {
            "recall_configured":      bool(recall.RECALL_API_KEY),
            "teams_bot_configured":   bool(recall.TEAMS_BOT_EMAIL and recall.TEAMS_BOT_PASSWORD),
            "slack_configured":       bool(slack.BOT_TOKEN),
            "jira_configured":        bool(jira.JIRA_URL and jira.JIRA_TOKEN),
        },
    }
