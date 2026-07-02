import asyncio
import json
import logging
from fastapi import APIRouter, HTTPException, Request, Form
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
from app.db import get_db, get_setting, set_setting, get_settings_bulk, set_settings_bulk
from app.apify_client import start_scrape, get_run_status, fetch_dataset
from app.proposal_writer import generate_proposal
from app.slack_notifier import (
    send_slack_message,
    format_scrape_complete_blocks,
)
from app.poller import poll_scrape

router = APIRouter()
logger = logging.getLogger(__name__)
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
    raw_url: str | None = None
    sort: str = "newest"
    experience_level: list[str] | None = None
    max_job_age_value: int | None = None
    max_job_age_unit: str = "HOURS"
    custom_filters: list[dict] | None = None
    locations: list[str] | None = None
    payment_verified: bool | None = None
    budget_min: int | None = None
    budget_max: int | None = None
    hourly_budget_min: int | None = None
    hourly_budget_max: int | None = None


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
    auto_scrape_enabled: str | None = None
    auto_scrape_interval: str | None = None
    auto_keywords: str | None = None
    auto_job_type: str | None = None
    auto_max_jobs: str | None = None
    min_budget: str | None = None
    min_hourly_rate: str | None = None
    instant_alert_threshold: str | None = None
    notion_api_key: str | None = None
    notion_database_id: str | None = None
    notion_templates_database_id: str | None = None
    app_base_url: str | None = None
    ab_test_strategy: str | None = None
    opencode_go_api_key: str | None = None
    upwork_profile: str | None = None


# ── Page routes ───────────────────────────────────────────────────────


@router.get("/", response_class=HTMLResponse)
async def index(request: Request):
    return TemplateResponse(_jinja_env.get_template("index.html"), {"request": request, "active_page": "dashboard"})


@router.get("/proposals", response_class=HTMLResponse)
async def proposals_page(request: Request):
    return TemplateResponse(_jinja_env.get_template("proposals.html"), {"request": request, "active_page": "proposals"})


@router.get("/settings", response_class=HTMLResponse)
async def settings_page(request: Request):
    return TemplateResponse(_jinja_env.get_template("settings.html"), {"request": request, "active_page": "settings"})


@router.get("/analytics", response_class=HTMLResponse)
async def analytics_page(request: Request):
    return TemplateResponse(_jinja_env.get_template("analytics.html"), {"request": request, "active_page": "analytics"})


# ── Scrape endpoints ─────────────────────────────────────────────────


@router.post("/api/scrapes")
async def create_scrape(req: ScrapeRequest):
    logger.info(f"create_scrape called: keywords={req.keywords}, max_jobs={req.max_jobs}, job_type={req.job_type}")
    logger.info(f"create_scrape advanced: raw_url={req.raw_url}, sort={req.sort}, exp={req.experience_level}, "
                f"age={req.max_job_age_value}{req.max_job_age_unit}, locations={req.locations}, "
                f"filters={req.custom_filters}, pv={req.payment_verified}")
    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO scrapes (keywords, max_jobs, job_type, status)
               VALUES (?, ?, ?, 'pending')""",
            (json.dumps(req.keywords), req.max_jobs, req.job_type),
        )
        scrape_id = cursor.lastrowid
        await db.commit()
    logger.info(f"create_scrape: inserted scrape #{scrape_id}")

    # Build Apify-compatible config dict
    scrape_config: dict = {
        "query": req.keywords[0] if req.keywords else "",
        "jobType": [req.job_type],
        "max_jobs": req.max_jobs,
        "sort": req.sort,
    }
    if req.raw_url:
        scrape_config["rawUrl"] = req.raw_url
    if req.experience_level:
        scrape_config["experienceLevel"] = req.experience_level
    if req.max_job_age_value is not None:
        scrape_config["maxJobAge"] = {
            "type": req.max_job_age_unit,
            "amount": req.max_job_age_value,
        }
    if req.custom_filters:
        scrape_config["customFilters"] = req.custom_filters
    if req.locations:
        scrape_config["locations"] = req.locations
    if req.payment_verified is not None:
        scrape_config["paymentVerified"] = req.payment_verified
    if req.budget_min is not None:
        scrape_config["budgetMin"] = req.budget_min
    if req.budget_max is not None:
        scrape_config["budgetMax"] = req.budget_max
    if req.hourly_budget_min is not None:
        scrape_config["hourlyBudgetMin"] = req.hourly_budget_min
    if req.hourly_budget_max is not None:
        scrape_config["hourlyBudgetMax"] = req.hourly_budget_max

    logger.info(f"create_scrape: scrape_config={json.dumps(scrape_config, default=str)}")
    asyncio.create_task(poll_scrape(scrape_id, scrape_config))
    logger.info(f"create_scrape: poll_scrape task created for scrape #{scrape_id}")
    return {"scrape_id": scrape_id, "status": "pending"}


@router.get("/api/scrapes")
async def list_scrapes():
    async with get_db() as db:
        rows = await db.execute(
            "SELECT * FROM scrapes ORDER BY created_at DESC LIMIT 50"
        )
        scrapes = [dict(r) for r in await rows.fetchall()]

    for s in scrapes:
        try:
            s["keywords"] = json.loads(s["keywords"])
        except (json.JSONDecodeError, TypeError):
            pass
    return scrapes


@router.get("/api/scrapes/{scrape_id}")
async def get_scrape(scrape_id: int):
    async with get_db() as db:
        row = await db.execute("SELECT * FROM scrapes WHERE id = ?", (scrape_id,))
        scrape = await row.fetchone()

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

    return result


@router.post("/api/scrapes/{scrape_id}/refresh")
async def refresh_scrape(scrape_id: int):
    async with get_db() as db:
        row = await db.execute("SELECT * FROM scrapes WHERE id = ?", (scrape_id,))
        scrape = await row.fetchone()

        if not scrape:
            raise HTTPException(404, "Scrape not found")

        scrape = dict(scrape)
        run_id = scrape.get("apify_run_id")
        if not run_id:
            raise HTTPException(400, "No Apify run associated with this scrape")

        try:
            status = await get_run_status(run_id)
        except Exception as e:
            raise HTTPException(502, f"Apify API error: {e}")

        if status["status"] == "SUCCEEDED":
            try:
                results = await fetch_dataset(run_id)
            except Exception as e:
                raise HTTPException(502, f"Apify dataset error: {e}")

            await db.execute(
                "UPDATE scrapes SET status='completed', result_count=?, results_json=? WHERE id=?",
                (len(results), json.dumps(results), scrape_id),
            )
            await db.commit()
            return {"status": "completed", "result_count": len(results), "results": results}

        await db.execute(
            "UPDATE scrapes SET status=? WHERE id=?",
            (status["status"].lower(), scrape_id),
        )
        await db.commit()
        return {"status": status["status"].lower()}


# ── Proposal endpoints ───────────────────────────────────────────────


@router.post("/api/proposals")
async def create_proposal(req: ProposalRequest):
    from app.config import OPENROUTER_API_KEY
    from app.proposal_templates import select_template_for_job

    defaults = await get_settings_bulk({
        "my_role": "freelancer",
        "my_skills": "",
        "model": "openai/gpt-4o",
    })

    my_role = req.my_role or defaults["my_role"]
    my_skills = req.my_skills or defaults["my_skills"]
    model = req.model or defaults["model"]

    if not OPENROUTER_API_KEY:
        raise HTTPException(400, "OPENROUTER_API_KEY not configured")

    template = await select_template_for_job(req.job_data)

    try:
        if template:
            text = await generate_proposal(
                req.job_data, my_role, my_skills, model,
                custom_system_prompt=template.get("system_prompt"),
                custom_user_template=template.get("user_template"),
                temperature=template.get("temperature", 0.7),
                max_tokens=template.get("max_tokens", 600),
            )
        else:
            text = await generate_proposal(req.job_data, my_role, my_skills, model)
    except Exception as e:
        raise HTTPException(502, f"OpenRouter API error: {e}")

    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO proposals (scrape_id, job_data, proposal_text, model_used, template_id)
               VALUES (0, ?, ?, ?, ?)""",
            (json.dumps(req.job_data), text, model, template.get("id") if template else None),
        )
        proposal_id = cursor.lastrowid
        await db.commit()

    slack_url = await get_setting("slack_webhook_url", "")
    if slack_url:
        from app.slack_notifier import format_proposal_ready_blocks
        job_title = req.job_data.get("title", "Untitled")
        budget = req.job_data.get("budget", "Unknown") or "Unknown"
        asyncio.create_task(
            send_slack_message(
                slack_url,
                f"Proposal drafted for: {job_title}",
                blocks=format_proposal_ready_blocks(job_title, budget, text),
            )
        )

    return {"proposal_id": proposal_id, "text": text, "model_used": model, "template_id": template.get("id") if template else None}


@router.get("/api/proposals")
async def list_proposals():
    async with get_db() as db:
        rows = await db.execute(
            "SELECT * FROM proposals ORDER BY created_at DESC LIMIT 50"
        )
        proposals = [dict(r) for r in await rows.fetchall()]

    for p in proposals:
        try:
            p["job_data"] = json.loads(p["job_data"])
        except (json.JSONDecodeError, TypeError):
            pass
    return proposals


@router.get("/api/proposals/{proposal_id}")
async def get_proposal(proposal_id: int):
    async with get_db() as db:
        row = await db.execute("SELECT * FROM proposals WHERE id = ?", (proposal_id,))
        proposal = await row.fetchone()

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
    async with get_db() as db:
        await db.execute("DELETE FROM proposals WHERE id = ?", (proposal_id,))
        await db.commit()
    return {"ok": True}


# ── Settings endpoints ────────────────────────────────────────────────

_SETTINGS_DEFAULTS = {
    "my_role": "freelancer",
    "my_skills": "",
    "model": "openai/gpt-4o",
    "slack_webhook_url": "",
    "auto_scrape_enabled": "false",
    "auto_scrape_interval": "60",
    "auto_keywords": "",
    "auto_job_type": "hourly",
    "auto_max_jobs": "50",
    "min_budget": "0",
    "min_hourly_rate": "0",
    "instant_alert_threshold": "75",
    "notion_api_key": "",
    "notion_database_id": "",
    "notion_templates_database_id": "",
    "app_base_url": "http://localhost:8000",
    "ab_test_strategy": "random",
    "opencode_go_api_key": "",
    "upwork_profile": "",
}


@router.get("/api/settings")
async def get_settings():
    return await get_settings_bulk(_SETTINGS_DEFAULTS)


@router.post("/api/settings")
async def update_settings(req: SettingsUpdate):
    updates = {k: v for k, v in req.model_dump().items() if v is not None}
    if updates:
        await set_settings_bulk(updates)
    return await get_settings_bulk(_SETTINGS_DEFAULTS)


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


@router.get("/api/scheduler/status")
async def scheduler_status():
    """Return current scheduler state and active/recent scrapes."""
    from app.main import get_scheduler_state

    state = get_scheduler_state()
    settings = await get_settings_bulk({
        "auto_scrape_enabled": "false",
        "auto_scrape_interval": "60",
        "auto_keywords": "",
    })

    async with get_db() as db:
        row = await db.execute(
            "SELECT COUNT(*) as cnt FROM scrapes WHERE status IN ('pending', 'running')"
        )
        active = (await row.fetchone())["cnt"]

        row = await db.execute(
            "SELECT COUNT(*) as cnt FROM scrapes"
        )
        total = (await row.fetchone())["cnt"]

    enabled = settings["auto_scrape_enabled"].lower() == "true"
    interval = int(settings["auto_scrape_interval"])

    next_run = None
    if enabled and state["last_run"]:
        from datetime import datetime, timezone, timedelta
        last = datetime.fromisoformat(state["last_run"])
        next_dt = last + timedelta(minutes=interval)
        next_run = next_dt.isoformat()

    return {
        "enabled": enabled,
        "interval_minutes": interval,
        "keywords": settings["auto_keywords"],
        "last_run": state["last_run"],
        "next_run": next_run,
        "active_scrapes": active,
        "total_scrapes": total,
    }


@router.post("/api/cron/trigger")
async def trigger_cron_scrape():
    """Manually trigger the hourly auto-scrape (used by cron jobs)."""
    from app.poller import run_hourly_scrape
    await run_hourly_scrape()
    return {"ok": True, "message": "Auto-scrape triggered"}


@router.post("/api/cleanup")
async def run_cleanup():
    """Run maintenance cleanup (seen_jobs older than 30 days)."""
    from app.poller import cleanup_seen_jobs
    await cleanup_seen_jobs()
    return {"ok": True, "message": "Cleanup completed"}


@router.post("/api/seen-jobs/clear")
async def clear_seen_jobs():
    """Clear the seen_jobs table so future scrapes show all jobs again."""
    async with get_db() as db:
        await db.execute("DELETE FROM seen_jobs")
        await db.commit()
    return {"ok": True, "message": "Cleared seen jobs history"}


@router.delete("/api/scrapes/{scrape_id}")
async def delete_scrape(scrape_id: int):
    """Delete a single scrape and its results."""
    async with get_db() as db:
        await db.execute("DELETE FROM scrapes WHERE id = ?", (scrape_id,))
        await db.commit()
    return {"ok": True}


@router.post("/api/scrapes/clear-old")
async def clear_old_scrapes():
    """Delete all completed/failed scrapes older than 7 days."""
    async with get_db() as db:
        cursor = await db.execute(
            "DELETE FROM scrapes WHERE status IN ('completed', 'failed') AND created_at < datetime('now', '-7 days')"
        )
        count = cursor.rowcount
        await db.commit()
    return {"ok": True, "deleted": count}


@router.post("/api/scrapes/clear-all-completed")
async def clear_all_completed():
    """Delete all completed and failed scrapes."""
    async with get_db() as db:
        cursor = await db.execute(
            "DELETE FROM scrapes WHERE status IN ('completed', 'failed')"
        )
        count = cursor.rowcount
        await db.commit()
    return {"ok": True, "deleted": count}


# ── Notion Integration ────────────────────────────────────────────────


@router.post("/api/jobs/{scrape_id}/{job_index}/save-to-notion")
async def save_job_to_notion(scrape_id: int, job_index: int):
    """Save a specific job to Notion."""
    async with get_db() as db:
        row = await db.execute("SELECT * FROM scrapes WHERE id = ?", (scrape_id,))
        scrape = await row.fetchone()

    if not scrape:
        raise HTTPException(404, "Scrape not found")

    results_json = scrape["results_json"]
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
    if not await is_configured():
        raise HTTPException(400, "Notion not configured. Set NOTION_API_KEY and NOTION_DATABASE_ID in settings.")

    page_id = await save_job_to_notion(job, score, scrape_id)
    if page_id:
        return {"ok": True, "notion_page_id": page_id}
    else:
        raise HTTPException(502, "Failed to save to Notion")


@router.get("/api/notion/generate-proposal", response_class=HTMLResponse)
async def generate_proposal_from_notion(page_id: str):
    """Read a job from Notion, generate a proposal, and write it back to the page."""
    from app.notion_sync import read_job_from_notion, write_proposal_to_notion

    job = await read_job_from_notion(page_id)
    if not job:
        return HTMLResponse(
            "<html><body><h2>Error</h2><p>Could not read job from Notion. Check the page ID and permissions.</p></body></html>",
            status_code=404,
        )

    from app.proposal_templates import select_template_for_job
    template = await select_template_for_job(job)

    system_prompt = template.get("system_prompt") if template else None
    user_template = template.get("user_template") if template else None
    temperature = float(template.get("temperature", 0.7)) if template else 0.7
    max_tokens = int(template.get("max_tokens", 600)) if template else 600

    settings = await get_settings_bulk({"my_role": "freelancer", "my_skills": ""})

    try:
        proposal = await generate_proposal(
            job,
            my_role=settings.get("my_role", "freelancer"),
            my_skills=settings.get("my_skills", ""),
            custom_system_prompt=system_prompt,
            custom_user_template=user_template,
            temperature=temperature,
            max_tokens=max_tokens,
        )
    except Exception as e:
        logger.exception("Proposal generation failed for Notion page %s", page_id)
        return HTMLResponse(
            f"<html><body><h2>Error</h2><p>Proposal generation failed: {e}</p></body></html>",
            status_code=500,
        )

    ok = await write_proposal_to_notion(page_id, proposal)
    if not ok:
        return HTMLResponse(
            "<html><body><h2>Error</h2><p>Generated proposal but failed to write it back to Notion.</p></body></html>",
            status_code=502,
        )

    title = job.get("title", "Unknown Job")
    return HTMLResponse(f"""\
<html><body style="font-family:system-ui;max-width:600px;margin:40px auto;padding:20px">
<h2>Proposal Generated</h2>
<p>A proposal for <strong>{title}</strong> has been written to the Notion page.</p>
<p>The page status has been updated to <em>Proposal Drafted</em>.</p>
<p><a href="https://notion.so/{page_id.replace('-', '')}">Open in Notion</a></p>
</body></html>""")


# ── Proposal Templates ────────────────────────────────────────────────


from app.proposal_templates import (
    get_templates, add_template, update_template, delete_template,
    select_template, get_ab_test_results,
)

@router.get("/api/templates")
async def list_templates():
    async with get_db() as db:
        return await get_templates(db)


@router.post("/api/templates")
async def create_template_api(req: dict):
    async with get_db() as db:
        return await add_template(db, req)


@router.patch("/api/templates/{template_id}")
async def update_template_api(template_id: str, req: dict):
    async with get_db() as db:
        template = await update_template(db, template_id, req)
    if not template:
        raise HTTPException(404, "Template not found")
    return template


@router.delete("/api/templates/{template_id}")
async def delete_template_api(template_id: str):
    async with get_db() as db:
        ok = await delete_template(db, template_id)
    if not ok:
        raise HTTPException(404, "Template not found")
    return {"ok": True}


@router.get("/api/templates/ab-results")
async def ab_results():
    async with get_db() as db:
        return await get_ab_test_results(db)


# ── Slack Interactive Webhook Handler ────────────────────────────────


@router.post("/api/slack/interactive")
async def slack_interactive(payload: str = Form(None)):
    """Handle Slack interactive payloads (button clicks)."""
    if not payload:
        raise HTTPException(400, "No payload")

    try:
        data = json.loads(payload)
    except json.JSONDecodeError:
        raise HTTPException(400, "Invalid JSON")

    for action in data.get("actions", []):
        action_id = action.get("action_id", "")
        value = action.get("value", "")

        if action_id == "generate_proposal":
            await _handle_slack_proposal(value)
        elif action_id == "regenerate_proposal":
            await _handle_slack_regenerate(value)
        elif action_id == "save_to_notion":
            await _handle_slack_save_notion(value)
        elif action_id == "dismiss_job":
            pass  # Just acknowledge
        elif action_id.startswith("generate_proposal_"):
            await _handle_slack_proposal(value)

    return {"ok": True}


async def _handle_slack_proposal(value: str):
    """Extract scrape/job from Slack button value and generate a proposal."""
    parts = value.split(":")
    if len(parts) != 2:
        return

    scrape_id, job_index = int(parts[0]), int(parts[1])

    async with get_db() as db:
        row = await db.execute("SELECT * FROM scrapes WHERE id = ?", (scrape_id,))
        scrape = await row.fetchone()
        if not scrape or not scrape["results_json"]:
            return

        results = json.loads(scrape["results_json"])
        if job_index >= len(results):
            return

        job = results[job_index]

        from app.proposal_templates import select_template_for_job
        defaults = await get_settings_bulk({
            "my_role": "freelancer",
            "my_skills": "",
            "model": "openai/gpt-4o",
        })

        template = await select_template_for_job(job)

        if template:
            text = await generate_proposal(
                job, defaults["my_role"], defaults["my_skills"], defaults["model"],
                custom_system_prompt=template.get("system_prompt"),
                custom_user_template=template.get("user_template"),
                temperature=template.get("temperature", 0.7),
                max_tokens=template.get("max_tokens", 600),
            )
        else:
            text = await generate_proposal(job, defaults["my_role"], defaults["my_skills"], defaults["model"])

        cursor = await db.execute(
            """INSERT INTO proposals (scrape_id, job_data, proposal_text, model_used, template_id)
               VALUES (0, ?, ?, ?, ?)""",
            (json.dumps(job), text, defaults["model"], template.get("id") if template else None),
        )
        proposal_id = cursor.lastrowid
        await db.commit()

    slack_url = await get_setting("slack_webhook_url", "")
    if slack_url:
        from app.slack_notifier import format_proposal_ready_blocks
        await send_slack_message(
            slack_url,
            f"Proposal drafted for: {job.get('title', 'Untitled')}",
            blocks=format_proposal_ready_blocks(
                job.get("title", "Untitled"),
                job.get("budget", "Unknown") or "Unknown",
                text,
                scrape_id=scrape_id,
                job_index=job_index,
                proposal_id=proposal_id,
            ),
        )


async def _handle_slack_regenerate(value: str):
    """Regenerate a proposal from a Slack button click and send the new version."""
    parts = value.split(":")
    if len(parts) != 2:
        return

    scrape_id, job_index = int(parts[0]), int(parts[1])

    async with get_db() as db:
        row = await db.execute("SELECT * FROM scrapes WHERE id = ?", (scrape_id,))
        scrape = await row.fetchone()
        if not scrape or not scrape["results_json"]:
            return

        results = json.loads(scrape["results_json"])
        if job_index >= len(results):
            return

        job = results[job_index]

        from app.proposal_templates import select_template_for_job
        defaults = await get_settings_bulk({
            "my_role": "freelancer",
            "my_skills": "",
            "model": "openai/gpt-4o",
        })

        template = await select_template_for_job(job)

        if template:
            text = await generate_proposal(
                job, defaults["my_role"], defaults["my_skills"], defaults["model"],
                custom_system_prompt=template.get("system_prompt"),
                custom_user_template=template.get("user_template"),
                temperature=template.get("temperature", 0.7),
                max_tokens=template.get("max_tokens", 600),
            )
        else:
            text = await generate_proposal(job, defaults["my_role"], defaults["my_skills"], defaults["model"])

        cursor = await db.execute(
            """INSERT INTO proposals (scrape_id, job_data, proposal_text, model_used, template_id)
               VALUES (0, ?, ?, ?, ?)""",
            (json.dumps(job), text, defaults["model"], template.get("id") if template else None),
        )
        proposal_id = cursor.lastrowid
        await db.commit()

    slack_url = await get_setting("slack_webhook_url", "")
    if slack_url:
        from app.slack_notifier import format_proposal_ready_blocks
        await send_slack_message(
            slack_url,
            f"Proposal regenerated for: {job.get('title', 'Untitled')}",
            blocks=format_proposal_ready_blocks(
                job.get("title", "Untitled"),
                job.get("budget", "Unknown") or "Unknown",
                text,
                scrape_id=scrape_id,
                job_index=job_index,
                proposal_id=proposal_id,
                header_text="Proposal Regenerated",
            ),
        )


async def _handle_slack_save_notion(value: str):
    """Save a job to Notion from a Slack button click."""
    parts = value.split(":")
    if len(parts) != 2:
        return

    scrape_id, job_index = int(parts[0]), int(parts[1])

    async with get_db() as db:
        row = await db.execute("SELECT * FROM scrapes WHERE id = ?", (scrape_id,))
        scrape = await row.fetchone()
        if not scrape or not scrape["results_json"]:
            return

        results = json.loads(scrape["results_json"])
        if job_index >= len(results):
            return

        job = results[job_index]
        score = job.get("_score", 0)

    from app.notion_sync import save_job_to_notion as notion_save, is_configured
    if not await is_configured():
        logger.warning("_handle_slack_save_notion: Notion not configured")
        return

    page_id = await notion_save(job, score, scrape_id)

    slack_url = await get_setting("slack_webhook_url", "")
    if slack_url:
        title = job.get("title", "Untitled")
        if page_id:
            await send_slack_message(slack_url, f"Saved to Notion: {title}")
        else:
            await send_slack_message(slack_url, f"Failed to save to Notion: {title}")


# ── Health & Maintenance ──────────────────────────────────────────────


@router.get("/api/health")
async def health_check():
    async with get_db() as db:
        try:
            cursor = await db.execute("SELECT 1")
            await cursor.fetchone()
            db_status = "ok"
        except Exception as e:
            db_status = f"error: {str(e)[:100]}"

        cursor = await db.execute("SELECT COUNT(*) as cnt FROM seen_jobs")
        seen_count = (await cursor.fetchone())["cnt"]

        cursor = await db.execute(
            "SELECT COUNT(*) as cnt FROM scrapes WHERE status='failed' AND created_at > datetime('now', '-24 hours')"
        )
        recent_failures = (await cursor.fetchone())["cnt"]

    from app.notion_sync import is_configured
    return {
        "status": "healthy" if db_status == "ok" else "degraded",
        "database": db_status,
        "seen_jobs_count": seen_count,
        "recent_failures_24h": recent_failures,
        "notion_configured": await is_configured(),
    }


@router.get("/api/analytics")
async def analytics_api():
    from app.analytics import get_dashboard_stats
    async with get_db() as db:
        return await get_dashboard_stats(db)
