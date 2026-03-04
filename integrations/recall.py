"""
Recall.ai integration for Agent Farm

Recall.ai provides ready-made meeting bots for Zoom, Teams, Google Meet, etc.
We use it to join meetings, get transcripts, and feed them to meeting_processor.

Required env vars:
  RECALL_API_KEY        API key from app.recall.ai/dashboard/api-keys
  RECALL_REGION         us-east-1 | us-west-2 | eu-central-1 | ap-northeast-1
                        (default: us-east-1)
  RECALL_WEBHOOK_SECRET whsec_... secret from app.recall.ai/dashboard/webhooks
"""

import base64
import hashlib
import hmac
import os
from datetime import datetime, timezone
from typing import Optional

import httpx

RECALL_API_KEY = os.environ.get("RECALL_API_KEY", "")
RECALL_REGION  = os.environ.get("RECALL_REGION", "us-east-1")
RECALL_WEBHOOK_SECRET = os.environ.get("RECALL_WEBHOOK_SECRET", "")

# Dedicated Teams bot account (service account, no MFA)
TEAMS_BOT_EMAIL    = os.environ.get("TEAMS_BOT_EMAIL", "")
TEAMS_BOT_PASSWORD = os.environ.get("TEAMS_BOT_PASSWORD", "")

BOT_NAME = "Meeting Intelligence"

def _base_url() -> str:
    return f"https://{RECALL_REGION}.recall.ai/api/v1"


def _headers() -> dict:
    return {
        "Authorization": f"Token {RECALL_API_KEY}",
        "Content-Type": "application/json",
    }


def _check(resp: httpx.Response, context: str = ""):
    if not resp.is_success:
        raise RuntimeError(
            f"Recall.ai error{' (' + context + ')' if context else ''}: "
            f"{resp.status_code} — {resp.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Webhook signature verification (Svix-compatible)
# ---------------------------------------------------------------------------

def verify_webhook_signature(payload: bytes, headers: dict) -> bool:
    """
    Verify a Recall.ai webhook signature using Svix-style HMAC-SHA256.
    Returns True if valid, False if invalid or no secret configured.
    """
    if not RECALL_WEBHOOK_SECRET:
        return True  # Not configured — skip verification

    secret = RECALL_WEBHOOK_SECRET.removeprefix("whsec_")
    try:
        secret_bytes = base64.b64decode(secret)
    except Exception:
        return False

    msg_id        = headers.get("webhook-id", "")
    msg_timestamp = headers.get("webhook-timestamp", "")
    msg_sig       = headers.get("webhook-signature", "")

    if not (msg_id and msg_timestamp and msg_sig):
        return False

    # Reject timestamps older than 5 minutes (replay protection)
    try:
        ts = int(msg_timestamp)
        age = abs(int(datetime.now(timezone.utc).timestamp()) - ts)
        if age > 300:
            return False
    except (ValueError, TypeError):
        return False

    signing_input = f"{msg_id}.{msg_timestamp}.{payload.decode()}".encode()
    computed = base64.b64encode(
        hmac.new(secret_bytes, signing_input, hashlib.sha256).digest()
    ).decode()

    # Signature header may contain multiple space-separated "v1,<sig>" values
    for sig_part in msg_sig.split(" "):
        _, _, sig_value = sig_part.partition(",")
        if hmac.compare_digest(computed, sig_value):
            return True

    return False


# ---------------------------------------------------------------------------
# Bot management
# ---------------------------------------------------------------------------

async def create_bot(
    meeting_url: str,
    webhook_url: str = "",
    join_at: Optional[datetime] = None,
    bot_name: str = BOT_NAME,
) -> dict:
    """
    Create a Recall.ai bot and send it to a meeting.

    Args:
        meeting_url:  Full Zoom/Teams/Meet URL
        webhook_url:  Our server URL for real-time events (optional — can use
                      dashboard-configured webhooks instead)
        join_at:      When to join (defaults to immediately)
        bot_name:     Display name shown in the meeting

    Returns:
        Bot object with `id` field
    """
    payload: dict = {
        "meeting_url": meeting_url,
        "bot_name": bot_name,
        "recording_config": {
            "transcript": {
                "provider": {"meeting_captions": {}}
            },
        },
        "automatic_leave": {
            "waiting_room_timeout": 300,     # Leave after 5 min in waiting room
            "noisy_environment_duration": 0, # Never leave due to silence
        },
    }

    if join_at:
        payload["join_at"] = join_at.isoformat()

    # Inject Teams credentials automatically for Teams meetings
    is_teams = "teams.microsoft.com" in meeting_url or "teams.live.com" in meeting_url
    if is_teams and TEAMS_BOT_EMAIL and TEAMS_BOT_PASSWORD:
        payload["ms_teams_login"] = {
            "email": TEAMS_BOT_EMAIL,
            "password": TEAMS_BOT_PASSWORD,
        }

    if webhook_url:
        payload["realtime_endpoints"] = [
            {
                "type": "webhook",
                "url": webhook_url,
                "events": [
                    "bot.status_change",
                    "transcript.data",
                ],
            }
        ]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{_base_url()}/bot/",
            json=payload,
            headers=_headers(),
        )
    _check(resp, "create_bot")
    return resp.json()


async def get_bot(bot_id: str) -> dict:
    """Get bot details and status."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.get(f"{_base_url()}/bot/{bot_id}/", headers=_headers())
    _check(resp, "get_bot")
    return resp.json()


async def remove_bot(bot_id: str):
    """Remove bot from meeting (leave early)."""
    async with httpx.AsyncClient(timeout=10) as client:
        resp = await client.delete(f"{_base_url()}/bot/{bot_id}/", headers=_headers())
    if resp.status_code not in (200, 204):
        raise RuntimeError(f"Recall.ai remove_bot error: {resp.status_code}")


async def get_transcript_url(bot_id: str) -> Optional[str]:
    """Get the download URL for a bot's transcript."""
    bot = await get_bot(bot_id)
    try:
        return bot["media_shortcuts"]["transcript"]["data"]["download_url"]
    except (KeyError, TypeError):
        return None


async def download_transcript(bot_id: str) -> list[dict]:
    """
    Download and return the full transcript for a completed bot session.
    Returns a list of speaker segments: [{"speaker": str, "words": [...]}]
    """
    url = await get_transcript_url(bot_id)
    if not url:
        return []

    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        resp = await client.get(url)
        resp.raise_for_status()

    data = resp.json()
    # Recall.ai returns a list of speaker segments
    return data if isinstance(data, list) else data.get("results", [])


# ---------------------------------------------------------------------------
# Transcript normalization
# ---------------------------------------------------------------------------

def normalize_transcript(segments: list[dict]) -> list[dict]:
    """
    Convert Recall.ai transcript segments to our normalized format:
    [{"speaker": str, "text": str, "timestamp": float}]
    """
    normalized = []
    for segment in segments:
        speaker = segment.get("speaker", "Unknown")
        words = segment.get("words", [])
        if not words:
            continue

        text = " ".join(w.get("text", "") for w in words if w.get("text", "").strip())
        start_time = words[0].get("start_time", 0.0)

        if text.strip():
            normalized.append({
                "speaker": speaker,
                "text": text.strip(),
                "timestamp": start_time,
            })

    return normalized


# ---------------------------------------------------------------------------
# Webhook payload parsing
# ---------------------------------------------------------------------------

def parse_webhook(payload: dict) -> Optional[dict]:
    """
    Parse a Recall.ai webhook payload.
    Returns a normalized event dict, or None if not actionable.

    Normalised event keys:
      type        "bot.done" | "bot.status_change" | "transcript.data" | other
      bot_id      str
      status_code str (for status events)
      segment     dict (for transcript.data events)
    """
    event = payload.get("event", "")
    data  = payload.get("data", {})

    bot_id = (
        data.get("bot", {}).get("id")
        or data.get("bot_id")
        or ""
    )

    if event == "bot.status_change":
        status = data.get("data", {})
        code = status.get("code", "")
        return {
            "type": "bot.done" if code == "done" else "bot.status_change",
            "bot_id": bot_id,
            "status_code": code,
            "sub_code": status.get("sub_code"),
        }

    if event == "bot.done":
        return {"type": "bot.done", "bot_id": bot_id, "status_code": "done"}

    if event == "transcript.data":
        segment = data.get("transcript", {})
        return {"type": "transcript.data", "bot_id": bot_id, "segment": segment}

    return {"type": event, "bot_id": bot_id}
