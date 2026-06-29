import asyncio
import json
import logging
from datetime import datetime, timedelta
from app.db import get_db, get_setting, set_setting
from app.apify_client import start_scrape, get_run_status, fetch_dataset
from app.slack_notifier import send_slack_message, format_scrape_complete_blocks

logger = logging.getLogger(__name__)

POLL_INTERVAL = 10
MAX_WAIT = 600

# Track last auto-scrape time
_last_auto_scrape: datetime | None = None


async def poll_scrape(
    scrape_id: int,
    keywords: list[str],
    max_jobs: int,
    job_type: str,
):
    """Background task: start Apify actor, poll until done, save results, notify Slack."""
    db = await get_db()

    # Start the Apify run
    try:
        run = await start_scrape(keywords, max_jobs, job_type)
        run_id = run["run_id"]
    except Exception as e:
        logger.error(f"poll_scrape {scrape_id}: start_scrape failed: {e}")
        await db.execute(
            "UPDATE scrapes SET status='failed', result_count=0, results_json='[]', error_message=? WHERE id=?",
            (str(e), scrape_id),
        )
        await db.commit()
        await db.close()
        return

    await db.execute(
        "UPDATE scrapes SET status='running', apify_run_id=? WHERE id=?",
        (run_id, scrape_id),
    )
    await db.commit()
    await db.close()

    # Poll until done
    elapsed = 0
    while elapsed < MAX_WAIT:
        await asyncio.sleep(POLL_INTERVAL)
        elapsed += POLL_INTERVAL

        try:
            status = await get_run_status(run_id)
        except Exception as e:
            logger.warning(f"poll_scrape {scrape_id}: get_run_status failed: {e}")
            continue

        if status["status"] == "SUCCEEDED":
            break
        elif status["status"] in ("FAILED", "ABORTED", "TIMED-OUT"):
            db = await get_db()
            await db.execute(
                "UPDATE scrapes SET status='failed' WHERE id=?", (scrape_id,)
            )
            await db.commit()
            await db.close()
            return

    # Fetch results
    try:
        results = await fetch_dataset(run_id)
    except Exception as e:
        logger.error(f"poll_scrape {scrape_id}: fetch_dataset failed: {e}")
        results = []

    # Deduplicate: filter out jobs we've already seen
    new_results = []
    already_seen = 0
    db = await get_db()
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

    # Save to DB
    await db.execute(
        "UPDATE scrapes SET status='completed', result_count=?, new_count=?, results_json=? WHERE id=?",
        (len(results), len(new_results), json.dumps(new_results), scrape_id),
    )
    await db.commit()
    await db.close()

    # Send Slack notification
    slack_url = await get_setting("slack_webhook_url", "")
    if slack_url:
        keyword_str = ", ".join(keywords[:3])
        if len(keywords) > 3:
            keyword_str += f" +{len(keywords) - 3} more"
        await send_slack_message(
            slack_url,
            f"🔍 {len(new_results)} new jobs for [{keyword_str}] (scraped {len(results)} total, {already_seen} already seen)",
            blocks=format_scrape_complete_blocks(keywords, len(new_results), len(results)),
        )


async def run_hourly_scrape():
    """Run the hourly auto-scrape using saved keyword settings."""
    global _last_auto_scrape

    # Rate limit: skip if last run was < 50 minutes ago
    if _last_auto_scrape and (datetime.utcnow() - _last_auto_scrape) < timedelta(minutes=50):
        logger.info("run_hourly_scrape: skipped (last run < 50min ago)")
        return

    _last_auto_scrape = datetime.utcnow()

    # Get saved keywords from settings
    keywords_raw = await get_setting("auto_keywords", "")
    if not keywords_raw:
        logger.info("run_hourly_scrape: no auto_keywords configured, skipping")
        return

    keywords = [k.strip() for k in keywords_raw.split(",") if k.strip()]
    if not keywords:
        logger.info("run_hourly_scrape: empty keywords, skipping")
        return

    job_type = await get_setting("auto_job_type", "hourly")
    max_jobs = int(await get_setting("auto_max_jobs", "50"))

    logger.info(f"run_hourly_scrape: starting auto-scrape for {keywords}")

    # Create a scrape record
    db = await get_db()
    cursor = await db.execute(
        """INSERT INTO scrapes (keywords, max_jobs, job_type, status)
           VALUES (?, ?, ?, 'pending')""",
        (json.dumps(keywords), max_jobs, job_type),
    )
    scrape_id = cursor.lastrowid
    await db.commit()
    await db.close()

    # Run the poller in background
    asyncio.create_task(poll_scrape(scrape_id, keywords, max_jobs, job_type))
    logger.info(f"run_hourly_scrape: created scrape #{scrape_id}")
