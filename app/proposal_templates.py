"""Proposal templates and A/B testing.

Users can create multiple proposal "skills" (templates with persona/role/skills).
The system rotates through them for A/B testing and tracks which performs better.
"""
from __future__ import annotations
import json
import logging
import random
from collections import Counter

logger = logging.getLogger(__name__)


async def get_templates(db) -> list[dict]:
    """Get all proposal templates."""
    row = await db.execute(
        "SELECT value FROM settings WHERE key = 'proposal_templates'"
    )
    result = await row.fetchone()
    if result and result["value"]:
        try:
            return json.loads(result["value"])
        except (json.JSONDecodeError, TypeError):
            pass
    return []


async def save_templates(db, templates: list[dict]):
    """Save proposal templates to settings."""
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES ('proposal_templates', ?)",
        (json.dumps(templates),),
    )


async def add_template(db, template: dict) -> dict:
    """Add a new proposal template.

    template = {
        "name": "Python Developer",
        "role": "Senior Python Developer",
        "skills": "Python, FastAPI, PostgreSQL, Docker",
        "system_prompt": "You are an expert...",
        "user_template": "Write a proposal...",
        "temperature": 0.7,
        "max_tokens": 600,
    }
    """
    templates = await get_templates(db)

    # Generate ID
    import uuid
    template["id"] = str(uuid.uuid4())[:8]
    template["created_at"] = __import__("datetime").datetime.utcnow().isoformat()
    template["stats"] = {"sent": 0, "responded": 0, "hired": 0}

    templates.append(template)
    await save_templates(db, templates)
    return template


async def update_template(db, template_id: str, updates: dict) -> dict | None:
    """Update a proposal template."""
    templates = await get_templates(db)
    for t in templates:
        if t.get("id") == template_id:
            t.update(updates)
            await save_templates(db, templates)
            return t
    return None


async def delete_template(db, template_id: str) -> bool:
    """Delete a proposal template."""
    templates = await get_templates(db)
    filtered = [t for t in templates if t.get("id") != template_id]
    if len(filtered) == len(templates):
        return False
    await save_templates(db, filtered)
    return True


async def select_template(db, strategy: str = "random") -> dict | None:
    """Select a template for a new proposal.

    Strategies:
    - "random": pick randomly
    - "round_robin": cycle through in order
    - "best": pick the one with highest response rate
    - "ab_test": weighted random favoring under-tested templates
    """
    templates = await get_templates(db)
    if not templates:
        return None

    if len(templates) == 1:
        return templates[0]

    if strategy == "random":
        return random.choice(templates)

    if strategy == "round_robin":
        row = await db.execute(
            "SELECT value FROM settings WHERE key = 'template_last_index'"
        )
        result = await row.fetchone()
        last_idx = int(result["value"]) if result and result["value"] else -1
        next_idx = (last_idx + 1) % len(templates)
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES ('template_last_index', ?)",
            (str(next_idx),),
        )
        return templates[next_idx]

    if strategy == "best":
        # Pick highest response rate
        best = max(templates, key=lambda t: (
            t.get("stats", {}).get("responded", 0) /
            max(t.get("stats", {}).get("sent", 1), 1)
        ))
        return best

    if strategy == "ab_test":
        # Weighted random: templates with fewer tests get higher weight
        weights = []
        for t in templates:
            stats = t.get("stats", {})
            sent = stats.get("sent", 0)
            # Untested templates get weight 10, tested ones get weight proportional to performance
            if sent == 0:
                weights.append(10)
            else:
                responded = stats.get("responded", 0)
                rate = responded / sent
                weights.append(max(1, rate * 10))

        total = sum(weights)
        r = random.uniform(0, total)
        cumulative = 0
        for i, w in enumerate(weights):
            cumulative += w
            if r <= cumulative:
                return templates[i]
        return templates[-1]

    return random.choice(templates)


async def record_template_outcome(db, template_id: str, outcome: str):
    """Record what happened with a proposal from this template.

    outcome: 'sent', 'responded', 'hired', 'rejected'
    """
    templates = await get_templates(db)
    for t in templates:
        if t.get("id") == template_id:
            stats = t.setdefault("stats", {"sent": 0, "responded": 0, "hired": 0})
            if outcome in stats:
                stats[outcome] += 1
            await save_templates(db, templates)
            return
    logger.warning(f"Template {template_id} not found for outcome recording")


async def get_ab_test_results(db) -> list[dict]:
    """Get A/B test results for all templates.

    Returns list sorted by response rate.
    """
    templates = await get_templates(db)
    results = []
    for t in templates:
        stats = t.get("stats", {})
        sent = stats.get("sent", 0)
        responded = stats.get("responded", 0)
        hired = stats.get("hired", 0)

        response_rate = round(responded / max(sent, 1) * 100, 1)
        hire_rate = round(hired / max(sent, 1) * 100, 1)

        results.append({
            "id": t.get("id"),
            "name": t.get("name", "Untitled"),
            "role": t.get("role", ""),
            "sent": sent,
            "responded": responded,
            "hired": hired,
            "response_rate": response_rate,
            "hire_rate": hire_rate,
        })

    results.sort(key=lambda r: r["response_rate"], reverse=True)
    return results
