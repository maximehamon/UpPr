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


def _get_settings_from_db() -> tuple[str, str]:
    """Try to get Notion settings from DB (user-configured via Settings page)."""
    try:
        import asyncio
        from app.db import get_db, get_setting
        loop = asyncio.get_event_loop()
        if NOTION_API_KEY and NOTION_DATABASE_ID:
            return NOTION_API_KEY, NOTION_DATABASE_ID
        # Check DB
        try:
            db = asyncio.run(get_db())
            api_key = asyncio.run(get_setting("notion_api_key", ""))
            db_id = asyncio.run(get_setting("notion_database_id", ""))
            asyncio.run(db.close())
            if api_key and db_id:
                return api_key, db_id
        except Exception:
            pass
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


def is_configured() -> bool:
    """Check if Notion integration is configured."""
    api_key, db_id = _get_settings_from_db()
    return bool(api_key and db_id)


async def save_job_to_notion(job: dict, score: int, scrape_id: int) -> str | None:
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
    if not is_configured():
        logger.debug("Notion not configured, skipping save")
        return None

    try:
        api_key, database_id = _get_settings_from_db()
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

        # Add description as a page body (first 1000 chars)
        desc = (job.get("description", "") or "")[:1000]
        children = [
            {
                "object": "block",
                "type": "paragraph",
                "paragraph": {
                    "rich_text": [{"text": {"content": desc}}]
                }
            }
        ] if desc else []

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


async def check_duplicate_in_notion(url: str) -> bool:
    """Check if a job URL already exists in the Notion database."""
    if not is_configured():
        return False

    try:
        api_key, database_id = _get_settings_from_db()
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
