"""
Confluence integration for Agent Farm

Required env vars (shared with Jira):
  JIRA_URL          https://yourcompany.atlassian.net
  JIRA_EMAIL        your-email@company.com
  JIRA_API_TOKEN    API token from id.atlassian.com/manage-profile/security/api-tokens

Supported spaces:
  SSEG — Secure Services Edge
"""

import base64
import os
from typing import Optional

import httpx

CONFLUENCE_URL = os.environ.get("JIRA_URL", "").rstrip("/") + "/wiki"
JIRA_EMAIL     = os.environ.get("JIRA_EMAIL", "")
JIRA_TOKEN     = os.environ.get("JIRA_API_TOKEN", "")

SSEG_SPACE = "SSEG"  # Secure Services Edge


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
            f"Confluence API error{' (' + context + ')' if context else ''}: "
            f"{resp.status_code} — {resp.text[:300]}"
        )


# ---------------------------------------------------------------------------
# Search & read
# ---------------------------------------------------------------------------

async def search(query: str, space_key: str = SSEG_SPACE, limit: int = 10) -> list[dict]:
    """Full-text search within a Confluence space. Returns list of page summaries."""
    cql = f'space="{space_key}" AND text ~ "{query}" AND type=page ORDER BY lastModified DESC'
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CONFLUENCE_URL}/rest/api/content/search",
            params={"cql": cql, "limit": limit, "expand": "version,ancestors"},
            headers=_headers(),
        )
    _check(resp, "search")
    results = resp.json().get("results", [])
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "url": f"{CONFLUENCE_URL.replace('/wiki', '')}/wiki{r['_links']['webui']}",
            "last_modified": r.get("version", {}).get("when", ""),
            "modified_by": r.get("version", {}).get("by", {}).get("displayName", ""),
        }
        for r in results
    ]


async def get_page(page_id: str) -> dict:
    """Get full content of a Confluence page by ID."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CONFLUENCE_URL}/rest/api/content/{page_id}",
            params={"expand": "body.storage,version,ancestors,children.page"},
            headers=_headers(),
        )
    _check(resp, "get_page")
    d = resp.json()
    return {
        "id": d["id"],
        "title": d["title"],
        "body_storage": d.get("body", {}).get("storage", {}).get("value", ""),
        "url": f"{CONFLUENCE_URL.replace('/wiki', '')}/wiki{d['_links']['webui']}",
        "last_modified": d.get("version", {}).get("when", ""),
        "modified_by": d.get("version", {}).get("by", {}).get("displayName", ""),
        "version": d.get("version", {}).get("number", 1),
        "children": [c["title"] for c in d.get("children", {}).get("page", {}).get("results", [])],
    }


async def get_page_by_title(title: str, space_key: str = SSEG_SPACE) -> dict:
    """Get a Confluence page by exact title within a space."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CONFLUENCE_URL}/rest/api/content",
            params={
                "spaceKey": space_key,
                "title": title,
                "expand": "body.storage,version,ancestors,children.page",
            },
            headers=_headers(),
        )
    _check(resp, "get_page_by_title")
    results = resp.json().get("results", [])
    if not results:
        raise ValueError(f"Page '{title}' not found in space {space_key}")
    d = results[0]
    return {
        "id": d["id"],
        "title": d["title"],
        "body_storage": d.get("body", {}).get("storage", {}).get("value", ""),
        "url": f"{CONFLUENCE_URL.replace('/wiki', '')}/wiki{d['_links']['webui']}",
        "last_modified": d.get("version", {}).get("when", ""),
        "modified_by": d.get("version", {}).get("by", {}).get("displayName", ""),
        "version": d.get("version", {}).get("number", 1),
    }


async def list_pages(space_key: str = SSEG_SPACE, limit: int = 50) -> list[dict]:
    """List pages in a Confluence space, most recently modified first."""
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.get(
            f"{CONFLUENCE_URL}/rest/api/content",
            params={
                "spaceKey": space_key,
                "type": "page",
                "limit": limit,
                "expand": "version",
            },
            headers=_headers(),
        )
    _check(resp, "list_pages")
    results = resp.json().get("results", [])
    return [
        {
            "id": r["id"],
            "title": r["title"],
            "url": f"{CONFLUENCE_URL.replace('/wiki', '')}/wiki{r['_links']['webui']}",
            "last_modified": r.get("version", {}).get("when", ""),
            "modified_by": r.get("version", {}).get("by", {}).get("displayName", ""),
        }
        for r in results
    ]


# ---------------------------------------------------------------------------
# Write
# ---------------------------------------------------------------------------

async def create_page(
    title: str,
    body: str,
    space_key: str = SSEG_SPACE,
    parent_id: Optional[str] = None,
) -> dict:
    """Create a new Confluence page with HTML or Confluence storage format body."""
    payload: dict = {
        "type": "page",
        "title": title,
        "space": {"key": space_key},
        "body": {
            "storage": {
                "value": body,
                "representation": "storage",
            }
        },
    }
    if parent_id:
        payload["ancestors"] = [{"id": parent_id}]

    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CONFLUENCE_URL}/rest/api/content",
            json=payload,
            headers=_headers(),
        )
    _check(resp, "create_page")
    d = resp.json()
    return {
        "id": d["id"],
        "title": d["title"],
        "url": f"{CONFLUENCE_URL.replace('/wiki', '')}/wiki{d['_links']['webui']}",
    }


async def update_page(
    page_id: str,
    title: str,
    body: str,
    version: int,
) -> dict:
    """Update an existing Confluence page. Version must be current version + 1."""
    payload = {
        "type": "page",
        "title": title,
        "version": {"number": version},
        "body": {
            "storage": {
                "value": body,
                "representation": "storage",
            }
        },
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.put(
            f"{CONFLUENCE_URL}/rest/api/content/{page_id}",
            json=payload,
            headers=_headers(),
        )
    _check(resp, "update_page")
    d = resp.json()
    return {
        "id": d["id"],
        "title": d["title"],
        "url": f"{CONFLUENCE_URL.replace('/wiki', '')}/wiki{d['_links']['webui']}",
        "version": d.get("version", {}).get("number"),
    }


async def add_comment(page_id: str, comment: str) -> dict:
    """Add a comment to a Confluence page."""
    payload = {
        "type": "comment",
        "container": {"id": page_id, "type": "page"},
        "body": {
            "storage": {
                "value": f"<p>{comment}</p>",
                "representation": "storage",
            }
        },
    }
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            f"{CONFLUENCE_URL}/rest/api/content",
            json=payload,
            headers=_headers(),
        )
    _check(resp, "add_comment")
    d = resp.json()
    return {"id": d["id"], "url": f"{CONFLUENCE_URL.replace('/wiki', '')}/wiki{d['_links']['webui']}"}
