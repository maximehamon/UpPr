import httpx
import json
import logging

logger = logging.getLogger(__name__)


async def send_slack_message(
    webhook_url: str,
    text: str,
    blocks: list[dict] | None = None,
) -> bool:
    """Send a message to a Slack incoming webhook.

    Returns True if successful, False otherwise.
    """
    if not webhook_url:
        return False

    payload = {"text": text}
    if blocks:
        payload["blocks"] = blocks

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            return resp.status_code == 200
    except Exception as e:
        logger.error(f"Slack send failed: {e}")
        return False


async def send_slack_interactive(
    webhook_url: str,
    text: str,
    actions: list[dict] | None = None,
) -> bool:
    """Send a message with interactive components (buttons) to Slack.

    actions: list of {"id": "...", "label": "...", "value": "...", "style": "primary|danger"}
    """
    if not webhook_url:
        return False

    blocks = [{"type": "section", "text": {"type": "mrkdwn", "text": text}}]

    if actions:
        elements = []
        for a in actions:
            btn = {
                "type": "button",
                "text": {"type": "plain_text", "text": a["label"]},
                "action_id": a["id"],
                "value": a.get("value", a["id"]),
            }
            if a.get("style") == "primary":
                btn["style"] = "primary"
            elif a.get("style") == "danger":
                btn["style"] = "danger"
            elements.append(btn)
        blocks.append({"type": "actions", "elements": elements})

    payload = {"text": text, "blocks": blocks}

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.post(webhook_url, json=payload)
            return resp.status_code == 200
    except Exception as e:
        logger.error(f"Slack interactive send failed: {e}")
        return False


def _escape_slack(text: str) -> str:
    """Escape special Slack mrkdwn characters."""
    for char in ["&", "<", ">"]:
        text = text.replace(char, f"\\{char}")
    return text


def format_scrape_complete_blocks(
    keywords: list[str],
    job_count: int,
    result_count: int,
    app_url: str = "",
) -> list[dict]:
    """Build Slack Block Kit payload for scrape completion notification."""
    kw_str = ", ".join(keywords[:5])
    if len(keywords) > 5:
        kw_str += f" +{len(keywords) - 5} more"

    if job_count == 0:
        header = "🔍 Upwork Scrape — No New Jobs"
        results_text = f"No new jobs found for [{_escape_slack(kw_str)}]"
    else:
        header = f"🔍 Upwork Scrape — {job_count} New Jobs!"
        results_text = f"{job_count} new jobs (scraped {result_count} total, duplicates removed)"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": header,
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Keywords:*\n{_escape_slack(kw_str)}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Results:*\n{results_text}",
                },
            ],
        },
    ]

    if app_url and job_count > 0:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View Results"},
                        "url": app_url,
                    }
                ],
            }
        )

    return blocks


def format_job_alert_blocks(
    job: dict,
    score: int,
    score_details: dict,
    scrape_id: int,
    job_index: int,
    app_url: str = "",
) -> tuple[str, list[dict]]:
    """Build Slack Block Kit for a single job alert with score and buttons.

    Returns (text, blocks) — text is the plain-text fallback.
    """
    title = job.get("title", "Untitled")
    budget = job.get("budget", "Not specified") or "Not specified"
    job_type = job.get("jobType", "N/A") or "N/A"
    client_country = job.get("clientCountry", "Unknown") or "Unknown"
    url = job.get("url") or job.get("listingUrl") or ""

    # Score emoji
    if score >= 80:
        score_emoji = "🟢"
    elif score >= 50:
        score_emoji = "🟡"
    else:
        score_emoji = "🔴"

    # Score details text
    score_parts = []
    if score_details.get("payment_verified"):
        score_parts.append("✅ Payment Verified")
    if score_details.get("good_budget"):
        score_parts.append("💰 Good Budget")
    if score_details.get("good_client"):
        score_parts.append("👍 Good Client")
    if score_details.get("low_competition"):
        score_parts.append("🎯 Low Competition")
    if score_details.get("red_flags"):
        for rf in score_details["red_flags"]:
            score_parts.append(f"⚠️ {rf}")

    score_text = "\n".join(score_parts) if score_parts else "No special signals"

    text = f"{score_emoji} New Job (Score: {score}/100): {title} — {budget}"

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"{score_emoji} {title}",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Budget:*\n{budget}"},
                {"type": "mrkdwn", "text": f"*Type:*\n{job_type}"},
                {"type": "mrkdwn", "text": f"*Country:*\n{client_country}"},
                {"type": "mrkdwn", "text": f"*Score:*\n{score_emoji} {score}/100"},
            ],
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Signals:*\n{score_text}",
            },
        },
    ]

    # Add description preview
    desc = (job.get("description", "") or "")[:200]
    if desc:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Preview:*\n{_escape_slack(desc)}...",
            },
        })

    # Action buttons
    actions = []
    if url:
        actions.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "🔗 Open on Upwork"},
            "url": url,
        })
    actions.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "✍️ Generate Proposal"},
        "action_id": "generate_proposal",
        "value": f"{scrape_id}:{job_index}",
        "style": "primary",
    })
    actions.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "📋 Save to Notion"},
        "action_id": "save_to_notion",
        "value": f"{scrape_id}:{job_index}",
    })
    actions.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "🗑 Dismiss"},
        "action_id": "dismiss_job",
        "value": f"{scrape_id}:{job_index}",
        "style": "danger",
    })

    blocks.append({"type": "actions", "elements": actions})

    return text, blocks


def format_proposal_ready_blocks(
    job_title: str,
    budget: str,
    proposal_text: str,
    app_url: str = "",
    scrape_id: int = 0,
    job_index: int = 0,
    proposal_id: int = 0,
    header_text: str = "Proposal Drafted",
) -> list[dict]:
    """Slack Block Kit for a new proposal being ready.

    The full proposal text is included in code blocks for easy copying.
    If the text exceeds Slack's 3000-char block limit, it is split across
    multiple section blocks.
    """
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"✍️ {header_text}",
            },
        },
        {
            "type": "section",
            "fields": [
                {
                    "type": "mrkdwn",
                    "text": f"*Job:*\n{_escape_slack(job_title)}",
                },
                {
                    "type": "mrkdwn",
                    "text": f"*Budget:*\n{_escape_slack(budget)}",
                },
            ],
        },
    ]

    # Split full proposal into code blocks that fit Slack's 3000-char limit.
    # Reserve room for the ``` delimiters (6 chars) + some margin.
    escaped = _escape_slack(proposal_text)
    max_chunk = 2900
    chunks = [escaped[i:i + max_chunk] for i in range(0, len(escaped), max_chunk)]

    for chunk in chunks:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```{chunk}```",
            },
        })

    # Action buttons
    action_value = f"{scrape_id}:{job_index}"
    actions = []

    actions.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "🔄 Regenerate"},
        "action_id": "regenerate_proposal",
        "value": action_value,
    })
    actions.append({
        "type": "button",
        "text": {"type": "plain_text", "text": "📋 Save to Notion"},
        "action_id": "save_to_notion",
        "value": action_value,
    })

    if app_url:
        actions.append({
            "type": "button",
            "text": {"type": "plain_text", "text": "🔗 View in App"},
            "url": app_url,
        })

    blocks.append({"type": "actions", "elements": actions})

    return blocks


def format_morning_recap_blocks(
    total_new: int,
    top_jobs: list[dict],
    keywords: list[str],
    app_url: str = "",
) -> list[dict]:
    """Build Slack Block Kit for morning recap of overnight jobs."""
    kw_str = ", ".join(keywords[:3])

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"☀️ Morning Recap — {total_new} New Jobs",
            },
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"Overnight results for [{_escape_slack(kw_str)}]:",
            },
        },
    ]

    for i, job in enumerate(top_jobs[:10]):
        title = job.get("title", "Untitled")
        budget = job.get("budget", "N/A") or "N/A"
        score = job.get("_score", 0)
        url = job.get("url") or job.get("listingUrl") or ""

        job_text = f"{i+1}. *{_escape_slack(title)}* — {budget}"
        if score:
            job_text += f" (Score: {score}/100)"

        if url:
            job_text = f"<{url}|{job_text}>"

        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": job_text},
            "accessory": {
                "type": "button",
                "text": {"type": "plain_text", "text": "✍️"},
                "action_id": f"generate_proposal_{i}",
                "value": str(job.get("_scrape_id", 0)) + ":" + str(job.get("_index", 0)),
            },
        })

    if app_url and total_new > 10:
        blocks.append({
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": f"View All {total_new} Jobs"},
                    "url": app_url,
                    "style": "primary",
                }
            ],
        })

    return blocks
