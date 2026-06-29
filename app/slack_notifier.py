import httpx
import json


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
    except Exception:
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

    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": f"🔍 Upwork Scrape Complete — {job_count} jobs found",
            },
        },
        {
            "type": "section",
            "fields": [
                {"type": "mrkdwn", "text": f"*Keywords:*\n{_escape_slack(kw_str)}"},
                {
                    "type": "mrkdwn",
                    "text": f"*Results:*\n{job_count} jobs scraped ({result_count} in dataset)",
                },
            ],
        },
    ]

    if app_url:
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


def format_proposal_ready_blocks(
    job_title: str,
    budget: str,
    preview: str,
    app_url: str = "",
) -> list[dict]:
    """Slack Block Kit for a new proposal being ready."""
    blocks = [
        {
            "type": "header",
            "text": {
                "type": "plain_text",
                "text": "✍️ Proposal Drafted",
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
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"```{_escape_slack(preview[:300])}```",
            },
        },
    ]

    if app_url:
        blocks.append(
            {
                "type": "actions",
                "elements": [
                    {
                        "type": "button",
                        "text": {"type": "plain_text", "text": "View Proposal"},
                        "url": app_url,
                    }
                ],
            }
        )

    return blocks
