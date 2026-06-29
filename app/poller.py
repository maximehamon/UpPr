import asyncio
import json
from app.db import get_db, get_setting
from app.apify_client import start_scrape, get_run_status, fetch_dataset
from app.slack_notifier import send_slack_message, format_scrape_complete_blocks

POLL_INTERVAL = 10
MAX_WAIT = 600


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
    except Exception:
        await db.execute(
            "UPDATE scrapes SET status='failed' WHERE id=?", (scrape_id,)
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
        except Exception:
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
    except Exception:
        results = []

    # Save to DB
    db = await get_db()
    await db.execute(
        "UPDATE scrapes SET status='completed', result_count=?, results_json=? WHERE id=?",
        (len(results), json.dumps(results), scrape_id),
    )
    await db.commit()
    await db.close()

    # Send Slack notification
    slack_url = await get_setting("slack_webhook_url", "")
    if slack_url:
        await send_slack_message(
            slack_url,
            f"Scrape complete: {len(results)} jobs found for [{', '.join(keywords)}]",
            blocks=format_scrape_complete_blocks(keywords, len(results), len(results)),
        )
