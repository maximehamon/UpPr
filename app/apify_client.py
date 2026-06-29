import logging
import httpx
import asyncio
from app.config import APIFY_API_KEY, APIFY_BASE_URL, ACTOR_ID

logger = logging.getLogger(__name__)

HEADERS = {"Authorization": f"Bearer {APIFY_API_KEY}", "Content-Type": "application/json"}


async def start_scrape(
    keywords: list[str], max_jobs: int = 50, job_type: str = "hourly"
) -> dict:
    """Start an Apify actor run. Returns {run_id, status}."""
    input_data = {
        "query": keywords[0] if keywords else "",
        "jobType": [job_type],
        "pagesToScrape": min(max_jobs // 50 + 1, 10),
        "perPage": 50,
        "sort": "newest",
    }
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{APIFY_BASE_URL}/acts/{ACTOR_ID}/runs",
            headers=HEADERS,
            json=input_data,
        )
        if resp.status_code >= 400:
            logger.error(f"Apify start_scrape HTTP {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        data = resp.json()["data"]
        return {"run_id": data["id"], "status": data["status"]}


async def get_run_status(run_id: str) -> dict:
    """Get current status of an actor run."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{APIFY_BASE_URL}/acts/{ACTOR_ID}/runs/{run_id}", headers=HEADERS
        )
        if resp.status_code >= 400:
            logger.error(f"Apify get_run_status HTTP {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        data = resp.json()["data"]
        return {
            "run_id": data["id"],
            "status": data["status"],
            "finished_at": data.get("finishedAt"),
        }


async def fetch_dataset(run_id: str, limit: int = 200) -> list[dict]:
    """Fetch all items from the run's default dataset."""
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{APIFY_BASE_URL}/acts/{ACTOR_ID}/runs/{run_id}/dataset/items"
            f"?limit={limit}",
            headers=HEADERS,
        )
        if resp.status_code >= 400:
            logger.error(f"Apify fetch_dataset HTTP {resp.status_code}: {resp.text}")
        resp.raise_for_status()
        return resp.json()


async def run_and_wait(
    keywords: list[str],
    max_jobs: int = 50,
    job_type: str = "hourly",
    poll_interval: int = 5,
    max_wait: int = 600,
) -> list[dict]:
    """Start a scrape, poll until done, return results."""
    run = await start_scrape(keywords, max_jobs, job_type)
    run_id = run["run_id"]
    elapsed = 0
    while elapsed < max_wait:
        await asyncio.sleep(poll_interval)
        elapsed += poll_interval
        status = await get_run_status(run_id)
        if status["status"] == "SUCCEEDED":
            return await fetch_dataset(run_id)
        elif status["status"] in ("FAILED", "ABORTED", "TIMED-OUT"):
            raise Exception(f"Apify run {run_id} ended: {status['status']}")
    raise TimeoutError(f"Run {run_id} did not finish within {max_wait}s")
