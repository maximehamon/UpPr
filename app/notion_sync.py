"""Notion integration for saving high-quality jobs.

Requires:
- NOTION_API_KEY (from notion.so/my-integrations)
- NOTION_DATABASE_ID (the database to save jobs to)

Install: pip install notion-client
"""
from __future__ import annotations
import logging
import os
from datetime import datetime

logger = logging.getLogger(__name__)

NOTION_API_KEY = os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")

# Lazy import so the app doesn't crash if notion-client isn't installed
_notion_client = None


async def _get_settings_from_db() -> tuple[str, str]:
    """Try to get Notion settings from DB (user-configured via Settings page)."""
    if NOTION_API_KEY and NOTION_DATABASE_ID:
        return NOTION_API_KEY, NOTION_DATABASE_ID
    try:
        from app.db import get_settings_bulk
        settings = await get_settings_bulk({
            "notion_api_key": "",
            "notion_database_id": "",
        })
        api_key = settings["notion_api_key"]
        db_id = settings["notion_database_id"]
        if api_key and db_id:
            return api_key, db_id
    except Exception:
        pass
    return NOTION_API_KEY, NOTION_DATABASE_ID


def _get_notion_client(auth_token: str | None = None):
    """Get or create a Notion client. Uses provided auth token, or falls back to env var."""
    global _notion_client
    if _notion_client is None:
        try:
            from notion_client import Client
            token = auth_token or NOTION_API_KEY
            _notion_client = Client(auth=token)
        except ImportError:
            logger.error("notion-client not installed. Run: pip install notion-client")
            raise
    return _notion_client


async def is_configured() -> bool:
    """Check if Notion integration is configured."""
    api_key, db_id = await _get_settings_from_db()
    return bool(api_key and db_id)


async def save_job_to_notion(job: dict, score: int, scrape_id: int, score_details: dict | None = None) -> str | None:
    """Save a job to Notion. Returns the page ID or None on failure.

    The Notion database should have these properties:
    - Title (title)
    - URL (url)
    - Budget (rich_text)
    - Job Type (select)
    - Client Country (rich_text)
    - Score (number)
    - Status (select): "New", "Applied", "Archived"
    - Scraped At (date)
    - Keywords (rich_text)
    """
    if not await is_configured():
        logger.debug("Notion not configured, skipping save")
        return None

    try:
        api_key, database_id = await _get_settings_from_db()
        client = _get_notion_client(api_key)

        properties = {
            "Title": {
                "title": [{"text": {"content": job.get("title", "Untitled")}}]
            },
            "URL": {
                "url": job.get("url") or job.get("listingUrl") or None
            },
            "Budget": {
                "rich_text": [{"text": {"content": str(job.get("budget", "N/A") or "N/A")}}]
            },
            "Job Type": {
                "select": {"name": (job.get("jobType", "Unknown") or "Unknown")}
            },
            "Client Country": {
                "rich_text": [{"text": {"content": job.get("clientCountry", "Unknown") or "Unknown"}}]
            },
            "Score": {
                "number": score
            },
            "Status": {
                "select": {"name": "New"}
            },
            "Scraped At": {
                "date": {"start": datetime.utcnow().strftime("%Y-%m-%d")}
            },
            "Keywords": {
                "rich_text": [{"text": {"content": f"Scrape #{scrape_id}"}}]
            },
        }

        # Build page body blocks
        children = []
        # Job description (first 1000 chars)
        desc = (job.get("description", "") or "")[:1000]
        if desc:
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": desc}}]}
            })

        # AI Score Analysis
        if score_details and score_details.get("reasoning"):
            children.append({
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "AI Score Analysis"}}]}
            })
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": score_details["reasoning"]}}]}
            })

            # Score breakdown
            breakdown = (
                f"Profile Fit: {score_details.get('profile_fit', 'N/A')}/25 | "
                f"Client: {score_details.get('client_quality', 'N/A')}/25 | "
                f"Budget: {score_details.get('budget_score', 'N/A')}/25 | "
                f"Competition: {score_details.get('competition', 'N/A')}/25"
            )
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": breakdown}}]}
            })

            for h in score_details.get("highlights", []):
                children.append({
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": [{"text": {"content": f"✅ {h}"}}]}
                })
            for rf in score_details.get("red_flags", []):
                children.append({
                    "object": "block",
                    "type": "bulleted_list_item",
                    "bulleted_list_item": {"rich_text": [{"text": {"content": f"⚠️ {rf}"}}]}
                })

        page = client.pages.create(
            parent={"database_id": database_id},
            properties=properties,
            children=children,
        )

        page_id = page.get("id", "")
        logger.info(f"Saved job to Notion: {page_id}")
        return page_id

    except Exception as e:
        logger.error(f"Failed to save job to Notion: {e}")
        return None


async def read_job_from_notion(page_id: str) -> dict | None:
    """Read a job's properties from a Notion page. Returns a dict compatible with proposal generation."""
    api_key, _ = await _get_settings_from_db()
    if not api_key:
        return None

    try:
        client = _get_notion_client(api_key)
        page = client.pages.retrieve(page_id=page_id)
        props = page.get("properties", {})

        def _rich_text(prop):
            return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))

        def _title(prop):
            return "".join(t.get("plain_text", "") for t in prop.get("title", []))

        # Read page body blocks for description
        blocks = client.blocks.children.list(block_id=page_id)
        description = ""
        for block in blocks.get("results", []):
            btype = block.get("type", "")
            if btype == "paragraph":
                text = "".join(t.get("plain_text", "") for t in block.get("paragraph", {}).get("rich_text", []))
                if text and "AI Score Analysis" not in text and "Profile Fit:" not in text:
                    description += text + "\n"
            if len(description) > 1500:
                break

        return {
            "title": _title(props.get("Title", {})),
            "url": props.get("URL", {}).get("url", ""),
            "budget": _rich_text(props.get("Budget", {})),
            "jobType": props.get("Job Type", {}).get("select", {}).get("name", ""),
            "clientCountry": _rich_text(props.get("Client Country", {})),
            "description": description.strip(),
            "_score": props.get("Score", {}).get("number", 0),
        }
    except Exception as e:
        logger.error(f"Failed to read job from Notion page {page_id}: {e}")
        return None


async def write_proposal_to_notion(page_id: str, proposal_text: str) -> bool:
    """Append a proposal as blocks to a Notion page."""
    api_key, _ = await _get_settings_from_db()
    if not api_key:
        return False

    try:
        client = _get_notion_client(api_key)

        children = [
            {
                "object": "block",
                "type": "divider",
                "divider": {},
            },
            {
                "object": "block",
                "type": "heading_2",
                "heading_2": {"rich_text": [{"text": {"content": "Generated Proposal"}}]},
            },
        ]

        # Notion blocks have a 2000-char limit per rich_text element
        for i in range(0, len(proposal_text), 1900):
            chunk = proposal_text[i:i + 1900]
            children.append({
                "object": "block",
                "type": "paragraph",
                "paragraph": {"rich_text": [{"text": {"content": chunk}}]},
            })

        client.blocks.children.append(block_id=page_id, children=children)

        # Update status to "Proposal Drafted"
        try:
            client.pages.update(
                page_id=page_id,
                properties={"Status": {"select": {"name": "Proposal Drafted"}}},
            )
        except Exception:
            pass

        logger.info(f"Wrote proposal to Notion page {page_id}")
        return True
    except Exception as e:
        logger.error(f"Failed to write proposal to Notion page {page_id}: {e}")
        return False


async def check_duplicate_in_notion(url: str) -> bool:
    """Check if a job URL already exists in the Notion database."""
    if not await is_configured():
        return False

    try:
        api_key, database_id = await _get_settings_from_db()
        client = _get_notion_client(api_key)
        response = client.databases.query(
            database_id=database_id,
            filter={
                "property": "URL",
                "url": {"equals": url}
            },
            page_size=1,
        )
        results = response.get("results", [])
        return len(results) > 0
    except Exception as e:
        logger.error(f"Notion duplicate check failed: {e}")
        return False
