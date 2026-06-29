"""Job quality scoring for Upwork jobs.

Scores jobs 0-100 based on:
- Budget / rate (0-30)
- Client quality (0-30)
- Competition signals (0-20)
- Red flags (-20 to 0)
"""
from __future__ import annotations
import logging

logger = logging.getLogger(__name__)


def score_job(job: dict, settings: dict | None = None) -> tuple[int, dict]:
    """Score a job 0-100. Returns (score, details_dict).

    settings may contain:
      - min_budget (int): minimum acceptable budget in USD
      - min_hourly_rate (int): minimum acceptable hourly rate in USD
    """
    if settings is None:
        settings = {}

    min_budget = settings.get("min_budget", 0)
    min_hourly_rate = settings.get("min_hourly_rate", 0)

    score = 50  # start neutral
    details = {
        "payment_verified": False,
        "good_budget": False,
        "good_client": False,
        "low_competition": False,
        "red_flags": [],
    }

    # ── Budget / Rate (0-30) ──────────────────────────────────────────
    budget = job.get("budget", None)
    hourly_rate = _parse_hourly_rate(job)
    job_type = (job.get("jobType", "") or "").lower()

    if job_type == "fixed" and budget:
        if budget >= 1000:
            score += 25
            details["good_budget"] = True
        elif budget >= 500:
            score += 15
        elif budget >= 100:
            score += 5
        elif budget < 50:
            score -= 10
            details["red_flags"].append(f"Low fixed budget (${budget})")
        if min_budget and budget >= min_budget:
            score += 5
    elif job_type == "hourly" and hourly_rate:
        if hourly_rate >= 80:
            score += 25
            details["good_budget"] = True
        elif hourly_rate >= 40:
            score += 15
        elif hourly_rate >= 20:
            score += 5
        elif hourly_rate < 15:
            score -= 10
            details["red_flags"].append(f"Low hourly rate (${hourly_rate}/hr)")
        if min_hourly_rate and hourly_rate >= min_hourly_rate:
            score += 5
    else:
        # Unknown type or no budget info — neutral
        pass

    # ── Client Quality (0-30) ─────────────────────────────────────────
    payment_verified = job.get("paymentVerified", None)
    if payment_verified is True:
        score += 15
        details["payment_verified"] = True
    elif payment_verified is False:
        score -= 10
        details["red_flags"].append("Payment not verified")

    total_spend = job.get("clientTotalSpend", None) or job.get("totalSpend", None)
    if total_spend:
        if total_spend >= 10000:
            score += 10
            details["good_client"] = True
        elif total_spend >= 1000:
            score += 5
        elif total_spend < 100:
            score -= 5
            details["red_flags"].append(f"Low client spend (${total_spend})")

    hires = job.get("clientTotalHires", None) or job.get("totalHires", None)
    if hires is not None:
        if hires >= 10:
            score += 5
        elif hires == 0:
            score -= 5
            details["red_flags"].append("Client never hired anyone")

    feedback = job.get("clientFeedbackScore", None) or job.get("feedback", None)
    if feedback is not None:
        if feedback >= 4.5:
            score += 5
        elif feedback < 3.5:
            score -= 5
            details["red_flags"].append(f"Low feedback ({feedback}/5)")

    # ── Competition (0-20) ────────────────────────────────────────────
    proposals = job.get("proposalsCount", None) or job.get("totalProposals", None)
    if proposals is not None:
        if proposals < 10:
            score += 15
            details["low_competition"] = True
        elif proposals < 25:
            score += 5
        elif proposals > 50:
            score -= 10
            details["red_flags"].append(f"High competition ({proposals} proposals)")

    # ── Red Flags (additional) ────────────────────────────────────────
    desc = (job.get("description", "") or "").lower()
    title = (job.get("title", "") or "").lower()

    spam_keywords = ["copy paste", "easy job", "simple task", "entry level only"]
    for kw in spam_keywords:
        if kw in desc or kw in title:
            score -= 5
            details["red_flags"].append(f"Possible spam signal: \"{kw}\"")
            break

    # Clamp
    score = max(0, min(100, score))

    return score, details


def _parse_hourly_rate(job: dict) -> float | None:
    """Extract numeric hourly rate from job data."""
    # Direct field
    rate = job.get("hourlyRate", None) or job.get("rate", None)
    if rate and isinstance(rate, (int, float)):
        return float(rate)

    # From budget string like "$40-60/hr" or "($50 /hr)"
    budget_str = job.get("budget", "") or ""
    if isinstance(budget_str, str) and ("/hr" in budget_str or "hour" in budget_str.lower()):
        import re
        numbers = re.findall(r"\\d+", budget_str)
        if numbers:
            rates = [float(n) for n in numbers]
            return sum(rates) / len(rates)

    return None
