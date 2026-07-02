import asyncio
import logging
import os
from datetime import datetime, timezone
from fastapi import FastAPI
from fastapi.staticfiles import StaticFiles
from app.routes import router

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)

logger = logging.getLogger(__name__)

app = FastAPI(title="Upwork Proposal Pipeline")

# Ensure static dir exists
os.makedirs("app/static", exist_ok=True)
app.mount("/static", StaticFiles(directory="app/static"), name="static")

# ── Background scheduler ────────────────────────────────────────────

_last_scheduled_run: datetime | None = None


async def _scheduler_loop():
    """Background loop that checks auto-scrape settings every 30s and runs if due."""
    global _last_scheduled_run
    logger.info("Scheduler loop started")

    while True:
        try:
            await asyncio.sleep(30)

            from app.db import get_settings_bulk
            settings = await get_settings_bulk({
                "auto_scrape_enabled": "false",
                "auto_scrape_interval": "60",
            })

            enabled = settings["auto_scrape_enabled"].lower() == "true"
            if not enabled:
                continue

            interval_minutes = int(settings["auto_scrape_interval"])
            now = datetime.now(timezone.utc)

            if _last_scheduled_run is not None:
                elapsed = (now - _last_scheduled_run).total_seconds() / 60
                if elapsed < interval_minutes:
                    continue

            logger.info(f"Scheduler: auto-scrape due (interval={interval_minutes}m), triggering")
            _last_scheduled_run = now

            from app.poller import run_hourly_scrape
            await run_hourly_scrape()

        except asyncio.CancelledError:
            logger.info("Scheduler loop cancelled")
            break
        except Exception:
            logger.exception("Scheduler loop error (will retry in 30s)")


@app.on_event("startup")
async def on_startup():
    from app.db import init_db
    await init_db()
    asyncio.create_task(_scheduler_loop())

# Include all routes
app.include_router(router)