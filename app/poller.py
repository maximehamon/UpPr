import asyncio
import json
import logging
from datetime import datetime, timedelta
from app.db import get_db, get_setting, get_settings_bulk
from app.apify_client import start_scrape, get_run_status, fetch_dataset
from app.slack_notifier import (
    send_slack_message,
    send_slack_interactive,
    format_scrape_complete_blocks,
    format_job_alert_blocks,
    format_morning_recap_blocks,
)
from app.job_scorer import score_job

logger = logging.getLogger(__name__)

POLL_INTERVAL = 10
MAX_WAIT = 600

_last_auto_scrape: datetime | None = None


async def poll_scrape(
    scrape_id: int,
    keywords: list[str],
    max_jobs: int,
    job_type: str,
):
    """Background task: start Apify actor, poll until done, save results, notify Slack."""
    try:
        await _poll_scrape_inner(scrape_id, keywords, max_jobs, job_type)
    except Exception as e:
        logger.exception(f"poll_scrape {scrape_id}: unhandled error: {e}")
        try:
            async with get_db() as db:
                await db.execute(
                    "UPDATE scrapes SET status='failed', error_message=? WHERE id=?",
                    (f"Internal error: {e}", scrape_id),
                )
                await db.commit()
        except Exception:
            pass


async def _poll_scrape_inner(
    scrape_id: int,
    keywords: list[str],
    max_jobs: int,
    job_type: str,
):
    # Start the Apify run
    try:
        run = await start_scrape(keywords, max_jobs, job_type)
        run_id = run["run_id"]
    except Exception as e:
        logger.error(f"poll_scrape {scrape_id}: start_scrape failed: {e}")
        async with get_db() as db:
            await db.execute(
                "UPDATE scrapes SET status='failed', result_count=0, results_json='[]', error_message=? WHERE id=?",
                (str(e), scrape_id),
            )
            await db.commit()
        return

    async with get_db() as db:
        await db.execute(
            "UPDATE scrapes SET status='running', apify_run_id=? WHERE id=?",
            (run_id, scrape_id),
        )
        await db.commit()

    # Poll until done
    elapsed = 0
    succeeded = False
    while elapsed < MAX_WAIT:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        try:
            status = await get_run_status(run_id)
        except Exception as e:
            logger.warning(f"poll_scrape {scrape_id}: get_run_status failed: {e}")
            continue

        if status["status"] == "SUCCEEDED":
            succeeded = True
            break
        elif status["status"] in ("FAILED", "ABORTED", "TIMED-OUT"):
            async with get_db() as db:
                await db.execute(
                    "UPDATE scrapes SET status='failed' WHERE id=?", (scrape_id,)
                )
                await db.commit()
            return

    if not succeeded:
        logger.error(f"poll_scrape {scrape_id}: timed out after {MAX_WAIT}s")
        async with get_db() as db:
            await db.execute(
                "UPDATE scrapes SET status='failed', error_message=? WHERE id=?",
                (f"Timed out after {MAX_WAIT}s waiting for Apify run", scrape_id),
            )
            await db.commit()
        return

    # Fetch results
    try:
        results = await fetch_dataset(run_id, limit=max_jobs)
    except Exception as e:
        logger.error(f"poll_scrape {scrape_id}: fetch_dataset failed: {e}")
        results = []

    # Deduplicate and score
    new_results = []
    already_seen = 0
    async with get_db() as db:
        for job in results:
            job_url = job.get("url") or job.get("listingUrl") or job.get("id") or ""
            if not job_url:
                new_results.append(job)
                continue
            row = await db.execute("SELECT 1 FROM seen_jobs WHERE url = ?", (job_url,))
            if await row.fetchone():
                already_seen += 1
            else:
                new_results.append(job)
                await db.execute("INSERT OR IGNORE INTO seen_jobs (url) VALUES (?)", (job_url,))

        if already_seen > 0:
            logger.info(f"poll_scrape {scrape_id}: filtered {already_seen} already-seen jobs, {len(new_results)} new")

        score_settings = await get_settings_bulk({
            "min_budget": "0",
            "min_hourly_rate": "0",
        })
        score_cfg = {
            "min_budget": int(score_settings["min_budget"]),
            "min_hourly_rate": int(score_settings["min_hourly_rate"]),
        }

        scored_jobs = []
        for job in new_results:
            score, details = score_job(job, score_cfg)
            job["_score"] = score
            job["_score_details"] = details
            scored_jobs.append(job)

        scored_jobs.sort(key=lambda j: j.get("_score", 0), reverse=True)

        await db.execute(
            "UPDATE scrapes SET status='completed', result_count=?, new_count=?, results_json=? WHERE id=?",
            (len(results), len(scored_jobs), json.dumps(scored_jobs), scrape_id),
        )
        await db.commit()

    # Smart alerts
    alert_settings = await get_settings_bulk({
        "slack_webhook_url": "",
        "instant_alert_threshold": "75",
        "app_base_url": "",
    })
    slack_url = alert_settings["slack_webhook_url"]

    if slack_url:
        instant_threshold = int(alert_settings["instant_alert_threshold"])
        high_jobs = [j for j in scored_jobs if j.get("_score", 0) >= instant_threshold]

        for i, job in enumerate(high_jobs[:3]):
            text, blocks = format_job_alert_blocks(
                job, job["_score"], job["_score_details"],
                scrape_id, i, app_url=alert_settings["app_base_url"],
            )
            await send_slack_interactive(slack_url, text, actions=[])

        keyword_str = ", ".join(keywords[:3])
        if len(keywords) > 3:
            keyword_str += f" +{len(keywords) - 3} more"

        if len(scored_jobs) > 0:
            summary_text = f"{len(scored_jobs)} new jobs for [{keyword_str}] — {len(high_jobs)} high priority"
            blocks = format_scrape_complete_blocks(keywords, len(scored_jobs), len(results))
            await send_slack_message(slack_url, summary_text, blocks=blocks)
        else:
            await send_slack_message(
                slack_url,
                f"No new jobs for [{keyword_str}] (scraped {len(results)} total, {already_seen} already seen)",
            )


async def run_hourly_scrape():
    """Run the hourly auto-scrape using saved keyword settings."""
    global _last_auto_scrape

    if _last_auto_scrape and (datetime.utcnow() - _last_auto_scrape) < timedelta(minutes=50):
        logger.info("run_hourly_scrape: skipped (last run < 50min ago)")
        return

    _last_auto_scrape = datetime.utcnow()

    settings = await get_settings_bulk({
        "auto_keywords": "",
        "auto_job_type": "hourly",
        "auto_max_jobs": "50",
    })

    keywords_raw = settings["auto_keywords"]
    if not keywords_raw:
        logger.info("run_hourly_scrape: no auto_keywords configured, skipping")
        return

    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    if not keywords:
        logger.info("run_hourly_scrape: empty keywords, skipping")
        return

    job_type = settings["auto_job_type"]
    max_jobs = int(settings["auto_max_jobs"])

    logger.info(f"run_hourly_scrape: starting auto-scrape for {keywords}")

    async with get_db() as db:
        cursor = await db.execute(
            """INSERT INTO scrapes (keywords, max_jobs, job_type, status)
               VALUES (?, ?, ?, 'pending')""",
            (json.dumps(keywords), max_jobs, job_type),
        )
        scrape_id = cursor.lastrowid
        await db.commit()

    asyncio.create_task(poll_scrape(scrape_id, keywords, max_jobs, job_type))
    logger.info(f"run_hourly_scrape: created scrape #{scrape_id}")


async def cleanup_seen_jobs():
    """Remove seen_jobs entries older than 30 days to prevent unbounded growth."""
    async with get_db() as db:
        await db.execute(
            "DELETE FROM seen_jobs WHERE first_seen_at < datetime('now', '-30 days')"
        )
        await db.commit()
    logger.info("cleanup_seen_jobs: removed entries older than 30 days")
