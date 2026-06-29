import asyncio
import json
from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel
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