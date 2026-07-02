"""Job quality scoring for Upwork jobs.

Uses LLM-based scoring via OpenCode Go API (DeepSeek V4 Flash) when
an API key is configured, otherwise falls back to a basic formula.

Scores jobs 0-100 with sub-scores for profile fit, client quality,
budget, and competition.
"""
from __future__ import annotations

import json
import logging
import re

import httpx

from app.config import OPENCODE_GO_API_KEY, OPENCODE_GO_BASE_URL, SCORING_MODEL

logger = logging.getLogger(__name__)

_SCORING_PROMPT = """\
You are a job-scoring assistant for an Upwork freelancer.
Analyze the job posting below against the freelancer's profile and return a JSON object with EXACTLY these keys:

- "score": integer 0-100 (overall quality)
- "profile_fit": integer 0-25 (how well the job matches the freelancer's skills/experience)
- "client_quality": integer 0-25 (based on payment verified, total spend, hire rate, feedback)
- "budget_score": integer 0-25 (whether the budget/rate is competitive)
- "competition": integer 0-25 (based on proposal count, job age — fewer proposals = higher score)
- "reasoning": string (1-2 sentence explanation)
- "red_flags": list of strings (any concerns)
- "highlights": list of strings (positive signals)

The overall "score" MUST equal profile_fit + client_quality + budget_score + competition.

Return ONLY valid JSON — no markdown, no explanation outside the JSON.

---
FREELANCER PROFILE:
{profile}
---
JOB POSTING:
Title: {title}
Type: {job_type}
Budget: {budget}
Description: {description}
Skills: {skills}
Project Length: {project_length}
Client Country: {client_country}
Payment Verified: {payment_verified}
Client Total Spend: ${client_total_spend}
Client Total Hires: {client_total_hires}
Client Feedback Score: {client_feedback}
Proposals So Far: {proposals_count}
"""


async def score_job(
    job: dict,
    upwork_profile: str = "",
    settings: dict | None = None,
) -> tuple[int, dict]:
    """Score a job 0-100 using LLM. Falls back to basic scorer on failure.

    Returns (score, details_dict).
    """
    if settings is None:
        settings = {}

    # Resolve API key: settings DB first, then env var
    api_key = settings.get("opencode_go_api_key", "") or OPENCODE_GO_API_KEY
    profile = upwork_profile or settings.get("upwork_profile", "") or ""

    if not api_key or not profile:
        logger.info(
            "LLM scoring unavailable (api_key=%s, profile=%s chars) — using basic scorer",
            "set" if api_key else "missing",
            len(profile),
        )
        return _score_job_basic(job, settings)

    try:
        return await _score_job_llm(job, profile, api_key)
    except Exception:
        logger.exception("LLM scoring failed — falling back to basic scorer")
        return _score_job_basic(job, settings)


async def _score_job_llm(
    job: dict, profile: str, api_key: str
) -> tuple[int, dict]:
    """Call OpenCode Go API for LLM-based scoring."""
    description = (job.get("description", "") or "")[:500]
    skills = job.get("skills", [])
    if isinstance(skills, list):
        skills_str = ", ".join(str(s) for s in skills)
    else:
        skills_str = str(skills or "")

    prompt = _SCORING_PROMPT.format(
        profile=profile[:800],
        title=job.get("title", "N/A"),
        job_type=job.get("jobType", "N/A"),
        budget=job.get("budget", "N/A"),
        description=description,
        skills=skills_str,
        project_length=job.get("projectLength", "N/A"),
        client_country=job.get("clientCountry", "N/A"),
        payment_verified=job.get("paymentVerified", "N/A"),
        client_total_spend=job.get("clientTotalSpend", 0),
        client_total_hires=job.get("clientTotalHires", "N/A"),
        client_feedback=job.get("clientFeedbackScore", "N/A"),
        proposals_count=job.get("proposalsCount", "N/A"),
    )

    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{OPENCODE_GO_BASE_URL}/chat/completions",
            headers={
                "Authorization": f"Bearer {api_key}",
                "Content-Type": "application/json",
            },
            json={
                "model": SCORING_MODEL,
                "messages": [{"role": "user", "content": prompt}],
                "temperature": 0.3,
                "max_tokens": 512,
            },
        )
        resp.raise_for_status()

    data = resp.json()
    message = data["choices"][0]["message"]
    raw_text = message.get("content") or ""

    # Some DeepSeek models put reasoning in a separate field and leave content empty
    if not raw_text.strip() and message.get("reasoning_content"):
        raw_text = message["reasoning_content"]

    logger.debug("LLM raw response (%d chars): %s", len(raw_text), raw_text[:300])

    # Strip <think>...</think> tags (DeepSeek reasoning)
    cleaned = re.sub(r"<think>.*?</think>", "", raw_text, flags=re.DOTALL).strip()
    # If stripping removed everything, the JSON was inside the think block
    if not cleaned:
        cleaned = raw_text

    # Extract JSON from possible markdown fences
    json_match = re.search(r"\{.*\}", cleaned, flags=re.DOTALL)
    if not json_match:
        raise ValueError(f"No JSON object found in LLM response: {cleaned[:300]}")

    result = json.loads(json_match.group())

    score = int(result.get("score", 50))
    score = max(0, min(100, score))

    details = {
        "profile_fit": int(result.get("profile_fit", 0)),
        "client_quality": int(result.get("client_quality", 0)),
        "budget_score": int(result.get("budget_score", 0)),
        "competition": int(result.get("competition", 0)),
        "reasoning": result.get("reasoning", ""),
        "red_flags": result.get("red_flags", []),
        "highlights": result.get("highlights", []),
        "scorer": "llm",
    }

    logger.info("LLM scored job %r → %d", job.get("title", "?")[:40], score)
    return score, details


# ── Fallback basic scorer ────────────────────────────────────────────


def _score_job_basic(job: dict, settings: dict | None = None) -> tuple[int, dict]:
    """Score a job 0-100 using a simple formula. Used as fallback.

    settings may contain:
      - min_budget (int): minimum acceptable budget in USD
      - min_hourly_rate (int): minimum acceptable hourly rate in USD
    """
    if settings is None:
        settings = {}

    min_budget = int(settings.get("min_budget", 0) or 0)
    min_hourly_rate = int(settings.get("min_hourly_rate", 0) or 0)

    score = 50  # start neutral
    details: dict = {
        "payment_verified": False,
        "good_budget": False,
        "good_client": False,
        "low_competition": False,
        "red_flags": [],
        "highlights": [],
        "reasoning": "Scored with basic formula (no LLM API key configured).",
        "scorer": "basic",
    }

    # ── Budget / Rate (0-30) ─────────────────────────────────────────
    raw_budget = job.get("budget", None)
    budget = _parse_number(raw_budget)
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

    # ── Client Quality (0-30) ────────────────────────────────────────
    payment_verified = job.get("paymentVerified", None)
    if payment_verified is True:
        score += 15
        details["payment_verified"] = True
    elif payment_verified is False:
        score -= 10
        details["red_flags"].append("Payment not verified")

    total_spend = _parse_number(job.get("clientTotalSpend") or job.get("totalSpend"))
    if total_spend:
        if total_spend >= 10000:
            score += 10
            details["good_client"] = True
        elif total_spend >= 1000:
            score += 5
        elif total_spend < 100:
            score -= 5
            details["red_flags"].append(f"Low client spend (${total_spend})")

    hires = _parse_number(job.get("clientTotalHires") or job.get("totalHires"))
    if hires is not None:
        if hires >= 10:
            score += 5
        elif hires == 0:
            score -= 5
            details["red_flags"].append("Client never hired anyone")

    feedback = _parse_number(job.get("clientFeedbackScore") or job.get("feedback"))
    if feedback is not None:
        if feedback >= 4.5:
            score += 5
        elif feedback < 3.5:
            score -= 5
            details["red_flags"].append(f"Low feedback ({feedback}/5)")

    # ── Competition (0-20) ───────────────────────────────────────────
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

    # ── Red Flags (additional) ───────────────────────────────────────
    desc = (job.get("description", "") or "").lower()
    title = (job.get("title", "") or "").lower()

    spam_keywords = ["copy paste", "easy job", "simple task", "entry level only"]
    for kw in spam_keywords:
        if kw in desc or kw in title:
            score -= 5
            details["red_flags"].append(f'Possible spam signal: "{kw}"')
            break

    # Clamp
    score = max(0, min(100, score))

    return score, details


def _parse_number(value) -> float | None:
    """Coerce a value to a number. Handles strings like '$1,500', '500', etc."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        return float(value)
    if isinstance(value, str):
        cleaned = re.sub(r"[^\d.]", "", value)
        if cleaned:
            try:
                return float(cleaned)
            except ValueError:
                pass
    return None


def _parse_hourly_rate(job: dict) -> float | None:
    """Extract numeric hourly rate from job data."""
    rate = job.get("hourlyRate", None) or job.get("rate", None)
    if rate and isinstance(rate, (int, float)):
        return float(rate)

    budget_str = job.get("budget", "") or ""
    if isinstance(budget_str, str) and ("/hr" in budget_str or "hour" in budget_str.lower()):
        numbers = re.findall(r"\d+", budget_str)
        if numbers:
            rates = [float(n) for n in numbers]
            return sum(rates) / len(rates)

    return None
