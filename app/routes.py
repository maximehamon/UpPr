import asyncio
import json
from fastapi import APIRouter, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from urllib.parse import parse_qs
from app.db import get_db, get_setting, set_setting
from app.apify_client import start_scrape, get_run_status, fetch_dataset
from app.proposal_writer import generate_proposal
from app.slack_notifier import (
    send_slack_message,
    format_scrape_complete_blocks,
)
from app.poller import poll_scrape

router = APIRouter()
import os
from jinja2 import Environment, FileSystemLoader
from starlette.templating import _TemplateResponse as TemplateResponse

_TEMPLATE_DIR = os.path.join(os.path.dirname(__file__), "templates")
_jinja_env = Environment(loader=FileSystemLoader(_TEMPLATE_DIR), autoescape=True)


# ── Pydantic models ──────────────────────────────────────────────────


class ScrapeRequest(BaseModel):
    keywords: list[str]
    max_jobs: int = 50
    job_type: str = "hourly"


class ProposalRequest(BaseModel):
    job_data: dict
    my_role: str | None = None
    my_skills: str | None = None
    model: str | None = None


class SettingsUpdate(BaseModel):
    my_role: str | None = None
    my_skills: str | None = None
    model: str | None = None
    slack_webhook_url: str | None = None
    auto_keywords: str | None = None
    auto_job_type: str | None = None
    auto_max_jobs: str | None = None
    min_budget: str | None = None
    min_hourly_rate: str | None = None
    instant_alert_threshold: str | None = None
    notion_api_key: str | None = None
    notion_database_id: str | None = None
    app_base_url: str | None = None


# ── Page routes ───────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TemplateResponse(_jinja_env.get_template("index.html"), {"request": request})


@router.get("/proposals", response_class=HTMLResponse)
async def proposals_page(request: Request):
    return TemplateResponse(_jinja_env.get_template("proposals.html"), {"request": request})


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return TemplateResponse(_jinja_env.get_template("settings.html"), {"request": request})


# ── Scrape endpoints ─────────────────────────────────────────────────


@router.post("/api/scrapes")
async def create_scrape(req: ScrapeRequest):
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO scrapes (keywords, max_jobs, job_type, status)
           VALUES (?, ?, ?, 'pending')""",
        (json.dumps(req.keywords), req.max_jobs, req.job_type),
    )
    scrape_id = cursor.lastrowid
    await db.commit()
    await db.close()

    # Fire-and-forget background poller
    asyncio.create_task(poll_scrape(scrape_id, req.keywords, req.max_jobs, req.job_type))

    return {"scrape_id": scrape_id, "status": "pending"}


@router.get("/api/scrapes")
async def list_scrapes():
    db = await get_db()
    rows = await db.execute(
        "SELECT * FROM scrapes ORDER BY created_at DESC LIMIT 50"
    )
    scrapes = [dict(r) for r in await rows.fetchall()]
    await db.close()

    for s in scrapes:
        try:
            s["keywords"] = json.loads(s["keywords"])
        except (json.JSONDecodeError, TypeError):
            pass
    return scrapes


@router.get("/api/scrapes/{scrape_id}")
async def get_scrape(scrape_id: int):
    db = await get_db()
    row = await db.execute("SELECT * FROM scrapes WHERE id = ?", (scrape_id,))
    scrape = await row.fetchone()
    await db.close()
    if not scrape:
        raise HTTPException(404, "Scrape not found")

    result = dict(scrape)
    try:
        result["keywords"] = json.loads(result["keywords"])
    except (json.JSONDecodeError, TypeError):
        pass

    if result.get("results_json"):
        try:
            result["results"] = json.loads(result["results_json"])
        except (json.JSONDecodeError, TypeError):
            result["results"] = []
    else:
        result["results"] = []

    # Expose error_message if present (don't strip it)
    return result


@router.post("/api/scrapes/{scrape_id}/refresh")
async def refresh_scrape(scrape_id: int):
    db = await get_db()
    row = await db.execute("SELECT * FROM scrapes WHERE id = ?", (scrape_id,))
    scrape = await row.fetchone()

    if not scrape:
        await db.close()
        raise HTTPException(404, "Scrape not found")

    scrape = dict(scrape)
    run_id = scrape.get("apify_run_id")
    if not run_id:
        await db.close()
        raise HTTPException(400, "No Apify run associated with this scrape")

    try:
        status = await get_run_status(run_id)
    except Exception as e:
        await db.close()
        raise HTTPException(502, f"Apify API error: {e}")

    if status["status"] == "SUCCEEDED":
        try:
            results = await fetch_dataset(run_id)
        except Exception as e:
            await db.close()
            raise HTTPException(502, f"Apify dataset error: {e}")

        await db.execute(
            "UPDATE scrapes SET status='completed', result_count=?, results_json=? WHERE id=?",
            (len(results), json.dumps(results), scrape_id),
        )
        await db.commit()
        await db.close()
        return {"status": "completed", "result_count": len(results), "results": results}

    await db.execute(
        "UPDATE scrapes SET status=? WHERE id=?",
        (status["status"].lower(), scrape_id),
    )
    await db.commit()
    await db.close()
    return {"status": status["status"].lower()}


# ── Proposal endpoints ───────────────────────────────────────────────


@router.post("/api/proposals")
async def create_proposal(req: ProposalRequest):
    from app.config import OPENROUTER_API_KEY

    my_role = req.my_role or await get_setting("my_role", "freelancer")
    my_skills = req.my_skills or await get_setting("my_skills", "")
    model = req.model or await get_setting("model", "openai/gpt-4o")

    if not OPENROUTER_API_KEY:
        raise HTTPException(400, "OPENROUTER_API_KEY not configured")

    try:
        text = await generate_proposal(req.job_data, my_role, my_skills, model)
    except Exception as e:
        raise HTTPException(502, f"OpenRouter API error: {e}")

    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO proposals (scrape_id, job_data, proposal_text, model_used)
           VALUES (0, ?, ?, ?)""",
        (json.dumps(req.job_data), text, model),
    )
    proposal_id = cursor.lastrowid
    await db.commit()
    await db.close()

    # Slack notification
    slack_url = await get_setting("slack_webhook_url", "")
    if slack_url:
        from app.slack_notifier import format_proposal_ready_blocks
        job_title = req.job_data.get("title", "Untitled")
        budget = req.job_data.get("budget", "Unknown") or "Unknown"
        asyncio.create_task(
            send_slack_message(
                slack_url,
                f"Proposal drafted for: {job_title}",
                blocks=format_proposal_ready_blocks(job_title, budget, text[:300]),
            )
        )

    return {"proposal_id": proposal_id, "text": text, "model_used": model}


@router.get("/api/proposals")
async def list_proposals():
    db = await get_db()
    rows = await db.execute(
        "SELECT * FROM proposals ORDER BY created_at DESC LIMIT 50"
    )
    proposals = [dict(r) for r in await rows.fetchall()]
    await db.close()
    for p in proposals:
        try:
            p["job_data"] = json.loads(p["job_data"])
        except (json.JSONDecodeError, TypeError):
            pass
    return proposals


@router.get("/api/proposals/{proposal_id}")
async def get_proposal(proposal_id: int):
    db = await get_db()
    row = await db.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
    proposal = await row.fetchone()
    await db.close()
    if not proposal:
        raise HTTPException(404, "Proposal not found")

    result = dict(proposal)
    try:
        result["job_data"] = json.loads(result["job_data"])
    except (json.JSONDecodeError, TypeError):
        pass
    return result


@router.delete("/api/proposals/{proposal_id}")
async def delete_proposal(proposal_id: int):
    db = await get_db()
    await db.execute("DELETE FROM proposals WHERE id = ?", (proposal_id,))
    await db.commit()
    await db.close()
    return {"ok": True}


# ── Settings endpoints ────────────────────────────────────────────────


@router.get("/api/settings")
async def get_settings():
    return {
        "my_role": await get_setting("my_role", "freelancer"),
        "my_skills": await get_setting("my_skills", ""),
        "model": await get_setting("model", "openai/gpt-4o"),
        "slack_webhook_url": await get_setting("slack_webhook_url", ""),
        "auto_keywords": await get_setting("auto_keywords", ""),
        "auto_job_type": await get_setting("auto_job_type", "hourly"),
        "auto_max_jobs": await get_setting("auto_max_jobs", "50"),
        "min_budget": await get_setting("min_budget", "0"),
        "min_hourly_rate": await get_setting("min_hourly_rate", "0"),
        "instant_alert_threshold": await get_setting("instant_alert_threshold", "75"),
        "notion_api_key": await get_setting("notion_api_key", ""),
        "notion_database_id": await get_setting("notion_database_id", ""),
        "app_base_url": await get_setting("app_base_url", "http://localhost:8000"),
    }


@router.post("/api/settings")
async def update_settings(req: SettingsUpdate):
    if req.my_role is not None:
        await set_setting("my_role", req.my_role)
    if req.my_skills is not None:
        await set_setting("my_skills", req.my_skills)
    if req.model is not None:
        await set_setting("model", req.model)
    if req.slack_webhook_url is not None:
        await set_setting("slack_webhook_url", req.slack_webhook_url)
    if req.auto_keywords is not None:
        await set_setting("auto_keywords", req.auto_keywords)
    if req.auto_job_type is not None:
        await set_setting("auto_job_type", req.auto_job_type)
    if req.auto_max_jobs is not None:
        await set_setting("auto_max_jobs", req.auto_max_jobs)
    if req.min_budget is not None:
        await set_setting("min_budget", req.min_budget)
    if req.min_hourly_rate is not None:
        await set_setting("min_hourly_rate", req.min_hourly_rate)
    if req.instant_alert_threshold is not None:
        await set_setting("instant_alert_threshold", req.instant_alert_threshold)
    if req.notion_api_key is not None:
        await set_setting("notion_api_key", req.notion_api_key)
    if req.notion_database_id is not None:
        await set_setting("notion_database_id", req.notion_database_id)
    if req.app_base_url is not None:
        await set_setting("app_base_url", req.app_base_url)

    return await get_settings()


# ── Config status ────────────────────────────────────────────────────


@router.get("/api/config-status")
async def config_status():
    from app.config import OPENROUTER_API_KEY, APIFY_API_KEY
    return {
        "apify_configured": bool(APIFY_API_KEY),
        "openrouter_configured": bool(OPENROUTER_API_KEY),
        "slack_configured": bool(await get_setting("slack_webhook_url", "")),
    }


# ── Cron / Auto-scrape ───────────────────────────────────────────────

@router.post("/api/cron/trigger")
async def trigger_cron_scrape():
    """Manually trigger the hourly auto-scrape (used by cron jobs)."""
    from app.poller import run_hourly_scrape
    await run_hourly_scrape()
    return {"ok": True, "message": "Auto-scrape triggered"}


@router.post("/api/seen-jobs/clear")
async def clear_seen_jobs():
    """Clear the seen_jobs table so future scrapes show all jobs again."""
    db = await get_db()
    await db.execute("DELETE FROM seen_jobs")
    await db.commit()
    await db.close()
    return {"ok": True, "message": "Cleared seen jobs history"}


# ── Notion Integration ────────────────────────────────────────────────

@router.post("/api/jobs/{scrape_id}/{job_index}/save-to-notion")
async def save_job_to_notion(scrape_id: int, job_index: int):
    """Save a specific job to Notion."""
    db = await get_db()
    row = await db.execute("SELECT * FROM scrapes WHERE id = ?", (scrape_id,))
    scrape = await row.fetchone()
    await db.close()

    if not scrape:
        raise HTTPException(404, "Scrape not found")

    results_json = scrape["results_json"] if hasattr(scrape, 'results_json') else scrape[10]
    if not results_json:
        raise HTTPException(400, "No results in this scrape")

    try:
        results = json.loads(results_json)
    except (json.JSONDecodeError, TypeError):
        raise HTTPException(500, "Invalid results data")

    if job_index >= len(results):
        raise HTTPException(404, "Job index out of range")

    job = results[job_index]
    score = job.get("_score", 0)

    from app.notion_sync import save_job_to_notion, is_configured
    if not is_configured():
        raise HTTPException(400, "Notion not configured. Set NOTION_API_KEY and NOTION_DATABASE_ID in settings.")

    page_id = await save_job_to_notion(job, score, scrape_id)
    if page_id:
        return {"ok": True, "notion_page_id": page_id}
    else:
        raise HTTPException(502, "Failed to save to Notion")


# ── Slack Interactive Webhook Handler ─────────────────────────────────

@router.post("/api/slack/interactive")
async def slack_interactive(payload: str = Form(None)):
    """Handle Slack interactive payloads (button clicks).

    Actions:
    - generate_proposal:{scrape_id}:{job_index} → creates proposal
    - dismiss_job:{scrape_id}:{job_index} → marks job as dismissed
    """
    if not payload:
        raise HTTPException(400, "No payload")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    # Handle actions
    actions = data.get("actions", [])
    for action in actions:
        action_id = action.get("action_id", "")
        value = action.get("value", "")

        if action_id == "generate_proposal":
            parts = value.split(":")
            if len(parts) == 2:
                scrape_id, job_index = int(parts[0]), int(parts[1])
                # Fetch the job and generate proposal
                db = await get_db()
                row = await db.execute("SELECT * FROM scrapes WHERE id = ?", (scrape_id,))
                scrape = await row.fetchone()
                if scrape:
                    results_json = scrape["results_json"] if hasattr(scrape, 'results_json') else scrape[10]
                    if results_json:
                        results = json.loads(results_json)
                        if job_index < len(results):
                            job = results[job_index]
                            from app.proposal_writer import generate_proposal
                            from app.config import OPENROUTER_API_KEY
                            my_role = await get_setting("my_role", "freelancer")
                            my_skills = await get_setting("my_skills", "")
                            model = await get_setting("model", "openai/gpt-4o")
                            text = await generate_proposal(job, my_role, my_skills, model)
                            # Save proposal
                            cursor = await db.execute(
                                """INSERT INTO proposals (scrape_id, job_data, proposal_text, model_used)
                                   VALUES (0, ?, ?, ?)""",
                                (json.dumps(job), text, model),
                            )
                            await db.commit()
                            # Notify Slack
                            slack_url = await get_setting("slack_webhook_url", "")
                            if slack_url:
                                from app.slack_notifier import format_proposal_ready_blocks
                                await send_slack_message(
                                    slack_url,
                                    f"✍️ Proposal drafted for: {job.get('title', 'Untitled')}",
                                    blocks=format_proposal_ready_blocks(
                                        job.get("title", "Untitled"),
                                        job.get("budget", "Unknown") or "Unknown",
                                        text[:300],
                                    ),
                                )
                await db.close()

        elif action_id == "dismiss_job":
            # Just acknowledge — the job stays in DB but won't be highlighted
            pass

    return {"ok": True}


# ── Health & Maintenance ──────────────────────────────────────────────

@router.get("/api/health")
async def health_check():
    """Health check endpoint."""
    db = await get_db()

    # Check DB connection
    try:
        await db.execute("SELECT 1")
        db_ok = True
    except Exception:
        db_ok = False

    # Get seen_jobs count
    row = await db.execute("SELECT COUNT(*) as cnt FROM seen_jobs")
    seen_count = (await row.fetchone())["cnt"] if db_ok else 0

    # Get consecutive failures
    row = await db.execute(
        "SELECT COUNT(*) as cnt FROM scrapes WHERE status='failed' AND created_at > datetime('now', '-24 hours')"
    )
    recent_failures = (await row.fetchone())["cnt"] if db_ok else 0

    await db.close()

    # Check Notion config
    from app.notion_sync import is_configured as notion_configured

    return {
        "status": "healthy" if db_ok else "unhealthy",
        "db_connected": db_ok,
        "seen_jobs_count": seen_count,
        "recent_failures_24h": recent_failures,
        "notion_configured": notion_configured(),
        "auto_keywords_configured": bool(await get_setting("auto_keywords", "")),
    }


@router.post("/api/cleanup")
async def run_cleanup():
    """Run maintenance cleanup (seen_jobs older than 30 days)."""
    from app.poller import cleanup_seen_jobs
    await cleanup_seen_jobs()
    return {"ok": True, "message": "Cleanup completed"}