"""Analytics for Upwork Pipeline.

Tracks and computes:
- Jobs per day trends
- Average quality score over time
- Best keywords by new job count
- Proposal performance (generated, sent, response rate)
- Client country distribution
- Budget distribution
"""
from __future__ import annotations
import json
import logging
from datetime import datetime, timedelta
from collections import Counter

logger = logging.getLogger(__name__)


async def get_dashboard_stats(db) -> dict:
    """Compute dashboard statistics from the database."""
    stats = {
        "total_scrapes": 0,
        "total_jobs_scraped": 0,
        "total_new_jobs": 0,
        "total_proposals": 0,
        "avg_score": 0,
        "scrapes_today": 0,
        "jobs_today": 0,
        "new_jobs_today": 0,
        "proposals_today": 0,
        "jobs_per_day": [],
        "score_distribution": {"high": 0, "medium": 0, "low": 0},
        "top_keywords": [],
        "top_countries": [],
        "budget_distribution": {"under_100": 0, "100_500": 0, "500_1000": 0, "1000_5000": 0, "over_5000": 0},
        "recent_failures": 0,
        "success_rate": 0,
    }

    # ── Basic counts ────────────────────────────────────────────────────
    row = await db.execute("SELECT COUNT(*) as cnt FROM scrapes")
    stats["total_scrapes"] = (await row.fetchone())["cnt"]

    row = await db.execute("SELECT COUNT(*) as cnt FROM scrapes WHERE status='completed'")
    completed_scrapes = (await row.fetchone())["cnt"]

    row = await db.execute("SELECT COALESCE(SUM(result_count), 0) as cnt FROM scrapes")
    stats["total_jobs_scraped"] = (await row.fetchone())["cnt"]

    row = await db.execute("SELECT COALESCE(SUM(new_count), 0) as cnt FROM scrapes")
    stats["total_new_jobs"] = (await row.fetchone())["cnt"]

    row = await db.execute("SELECT COUNT(*) as cnt FROM proposals")
    stats["total_proposals"] = (await row.fetchone())["cnt"]

    # ── Today's stats ───────────────────────────────────────────────────
    today_start = datetime.utcnow().strftime("%Y-%m-%d")
    row = await db.execute(
        "SELECT COUNT(*) as cnt FROM scrapes WHERE created_at LIKE ?", (today_start + "%",)
    )
    stats["scrapes_today"] = (await row.fetchone())["cnt"]

    row = await db.execute(
        "SELECT COALESCE(SUM(result_count), 0) as cnt FROM scrapes WHERE created_at LIKE ?",
        (today_start + "%",),
    )
    stats["jobs_today"] = (await row.fetchone())["cnt"]

    row = await db.execute(
        "SELECT COALESCE(SUM(new_count), 0) as cnt FROM scrapes WHERE created_at LIKE ?",
        (today_start + "%",),
    )
    stats["new_jobs_today"] = (await row.fetchone())["cnt"]

    row = await db.execute(
        "SELECT COUNT(*) as cnt FROM proposals WHERE created_at LIKE ?", (today_start + "%",)
    )
    stats["proposals_today"] = (await row.fetchone())["cnt"]

    # ── Success rate ────────────────────────────────────────────────────
    if stats["total_scrapes"] > 0:
        row = await db.execute("SELECT COUNT(*) as cnt FROM scrapes WHERE status='failed'")
        failed = (await row.fetchone())["cnt"]
        stats["recent_failures"] = failed
        stats["success_rate"] = round(
            (stats["total_scrapes"] - failed) / stats["total_scrapes"] * 100, 1
        )

    # ── Jobs per day (last 14 days) ────────────────────────────────────
    jobs_per_day = []
    for i in range(13, -1, -1):
        day = (datetime.utcnow() - timedelta(days=i)).strftime("%Y-%m-%d")
        row = await db.execute(
            "SELECT COALESCE(SUM(new_count), 0) as cnt FROM scrapes WHERE created_at LIKE ? AND status='completed'",
            (day + "%",),
        )
        count = (await row.fetchone())["cnt"]
        jobs_per_day.append({"date": day, "count": count})
    stats["jobs_per_day"] = jobs_per_day

    # ── Average score & distribution ───────────────────────────────────
    row = await db.execute(
        "SELECT results_json FROM scrapes WHERE status='completed' AND results_json IS NOT NULL AND results_json != '[]'"
    )
    all_results = await row.fetchall()
    scores = []
    for r in all_results:
        try:
            jobs = json.loads(r["results_json"])
            for job in jobs:
                if "_score" in job:
                    scores.append(job["_score"])
        except (json.JSONDecodeError, TypeError):
            continue

    if scores:
        stats["avg_score"] = round(sum(scores) / len(scores), 1)
        stats["score_distribution"] = {
            "high": sum(1 for s in scores if s >= 75),
            "medium": sum(1 for s in scores if 50 <= s < 75),
            "low": sum(1 for s in scores if s < 50),
        }

    # ── Top keywords ───────────────────────────────────────────────────
    row = await db.execute(
        "SELECT keywords FROM scrapes WHERE status='completed'"
    )
    keyword_rows = await row.fetchall()
    keyword_counter = Counter()
    for r in keyword_rows:
        try:
            kws = json.loads(r["keywords"])
            for kw in kws:
                keyword_counter[kw.strip().lower()] += 1
        except (json.JSONDecodeError, TypeError):
            continue
    stats["top_keywords"] = keyword_counter.most_common(10)

    # ── Top countries ──────────────────────────────────────────────────
    row = await db.execute(
        "SELECT results_json FROM scrapes WHERE status='completed' AND results_json IS NOT NULL AND results_json != '[]'"
    )
    country_counter = Counter()
    budget_counter = Counter()
    for r in all_results:
        try:
            jobs = json.loads(r["results_json"])
            for job in jobs:
                country = job.get("clientCountry", "Unknown") or "Unknown"
                country_counter[country] += 1

                budget = job.get("budget", None)
                if budget and isinstance(budget, (int, float)):
                    if budget < 100:
                        budget_counter["under_100"] += 1
                    elif budget < 500:
                        budget_counter["100_500"] += 1
                    elif budget < 1000:
                        budget_counter["500_1000"] += 1
                    elif budget < 5000:
                        budget_counter["1000_5000"] += 1
                    else:
                        budget_counter["over_5000"] += 1
        except (json.JSONDecodeError, TypeError):
            continue

    stats["top_countries"] = country_counter.most_common(8)
    stats["budget_distribution"] = dict(budget_counter)

    # ── Proposal performance ───────────────────────────────────────────
    row = await db.execute(
        """SELECT model_used, COUNT(*) as cnt FROM proposals 
           GROUP BY model_used ORDER BY cnt DESC LIMIT 5"""
    )
    model_rows = await row.fetchall()
    stats["models_usage"] = [{"model": r["model_used"], "count": r["cnt"]} for r in model_rows]

    return stats
