"""Proposal templates fetched from a Notion database.

Templates live in a Notion database so the user can edit them from Notion.
Each template has criteria keywords that determine when it gets used for a job.
"""
from __future__ import annotations
import logging
import random
import time

logger = logging.getLogger(__name__)

# ── Module-level cache ───────────────────────────────────────────────
_cache: list[dict] = []
_cache_ts: float = 0.0
_CACHE_TTL = 300  # 5 minutes


def _extract_rich_text(prop: dict) -> str:
    """Extract plain text from a Notion rich_text property."""
    return "".join(t.get("plain_text", "") for t in prop.get("rich_text", []))


async def fetch_templates_from_notion() -> list[dict]:
    """Fetch active templates from the Notion templates database.

    Results are cached for 5 minutes.  Returns an empty list when Notion
    is not configured or unreachable.
    """
    global _cache, _cache_ts

    if _cache and (time.time() - _cache_ts) < _CACHE_TTL:
        return _cache

    from app.db import get_settings_bulk

    keys = {
        "notion_api_key": "",
        "notion_templates_database_id": "",
    }
    settings = await get_settings_bulk(keys)
    api_key = settings.get("notion_api_key", "")
    db_id = settings.get("notion_templates_database_id", "")

    if not api_key or not db_id:
        logger.debug("Notion templates not configured — missing api_key or database_id")
        return []

    try:
        from notion_client import Client

        notion = Client(auth=api_key)

        # Query for active templates only
        response = notion.databases.query(
            database_id=db_id,
            filter={"property": "Active", "checkbox": {"equals": True}},
        )

        templates: list[dict] = []
        for page in response.get("results", []):
            props = page.get("properties", {})

            # Extract title (Name)
            name_parts = props.get("Name", {}).get("title", [])
            name = "".join(t.get("plain_text", "") for t in name_parts)

            system_prompt = _extract_rich_text(props.get("System Prompt", {}))
            user_template = _extract_rich_text(props.get("User Template", {}))
            criteria = _extract_rich_text(props.get("Criteria", {}))

            temperature = props.get("Temperature", {}).get("number")
            max_tokens = props.get("Max Tokens", {}).get("number")

            templates.append({
                "id": page["id"],
                "name": name,
                "system_prompt": system_prompt,
                "user_template": user_template,
                "criteria": criteria,
                "temperature": temperature if temperature is not None else 0.7,
                "max_tokens": int(max_tokens) if max_tokens is not None else 600,
            })

        _cache = templates
        _cache_ts = time.time()
        logger.info("Fetched %d active templates from Notion", len(templates))
        return templates

    except Exception:
        logger.exception("Failed to fetch templates from Notion")
        return _cache if _cache else []


async def select_template_for_job(job: dict) -> dict | None:
    """Pick the best-matching template for a job based on criteria keywords.

    Each template's criteria field is a comma-separated list of keywords.
    Templates are scored by how many keywords appear in the job's title,
    description, or skills.  The highest-scoring template wins; ties are
    broken randomly.  Returns None when no templates exist or none match.
    """
    templates = await fetch_templates_from_notion()
    if not templates:
        return None

    job_text = " ".join([
        (job.get("title") or ""),
        (job.get("description") or ""),
        " ".join(job.get("skills", []) if isinstance(job.get("skills"), list) else [str(job.get("skills", ""))]),
    ]).lower()

    scored: list[tuple[int, dict]] = []
    for tpl in templates:
        criteria_raw = tpl.get("criteria", "")
        if not criteria_raw.strip():
            # Template with no criteria is a universal fallback (score 0)
            scored.append((0, tpl))
            continue

        keywords = [kw.strip().lower() for kw in criteria_raw.split(",") if kw.strip()]
        score = sum(1 for kw in keywords if kw in job_text)
        if score > 0:
            scored.append((score, tpl))

    if not scored:
        return None

    max_score = max(s for s, _ in scored)
    best = [tpl for s, tpl in scored if s == max_score]
    chosen = random.choice(best)
    logger.info(
        "Selected template '%s' (score %d) for job '%s'",
        chosen.get("name"),
        max_score,
        job.get("title", "")[:60],
    )
    return chosen


# ── Backward-compat stubs (routes still reference these) ─────────────

async def get_templates(db) -> list[dict]:
    """Stub — templates now live in Notion."""
    return []


async def add_template(db, template: dict) -> dict:
    """Stub — templates now managed in Notion."""
    return {}


async def update_template(db, template_id: str, updates: dict) -> dict | None:
    """Stub — templates now managed in Notion."""
    return None


async def delete_template(db, template_id: str) -> bool:
    """Stub — templates now managed in Notion."""
    return False


async def select_template(db, strategy: str = "random") -> dict | None:
    """Stub — use select_template_for_job instead."""
    return None


async def record_template_outcome(db, template_id: str, outcome: str):
    """No-op — outcome tracking removed."""
    return


async def get_ab_test_results(db) -> list[dict]:
    """No-op — A/B testing removed."""
    return []
