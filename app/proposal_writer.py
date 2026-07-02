import re
import httpx
from app.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, PROPOSAL_MODEL, APP_BASE_URL

HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": APP_BASE_URL,
    "X-Title": "Upwork Proposal Pipeline",
}

PROPOSAL_SYSTEM_PROMPT = """\
ROLE
You write Upwork proposal cover letters that win interviews. Follow every rule below exactly. Do not add your own ideas about what makes a good proposal.

HARD CONSTRAINTS
- Length: two to three short paragraphs. Never longer. If it won't fit, cut, don't expand.
- The first two sentences carry the proposal. Only those show in the client's results-list preview, so they must hook and prove relevance immediately, not introduce the freelancer generically ("I'd love to work with you" is a wasted opening).
- Write every proposal from scratch for the specific job. Reference something concrete from the job post in the opening line. No reusable filler.
- Address the client by first name if one is supplied or findable. Otherwise use a plain greeting (Hi, Hello). Never invent a name.

STRUCTURE, in this order
1. Opening (1-2 sentences): Prove you read the post. Restate their core problem in your own words, or name something specific they asked for. Lead with the single strongest proof point (a named client, a result, a metric), not a self-introduction.
2. Middle (one short paragraph): Proof of relevant past work that matches their exact need. Use concrete detail: named publications or clients, hard numbers (e.g. "50,000-image project, 98.7% accuracy after review"), specific tools, clear niche fit. Reference work samples if links are provided.
3. Close (1-2 sentences): State turnaround time and/or rate only if it helps. End with ONE low-friction question that invites a reply ("Would you be open to a quick call this week to go over scope?"). Never end on "let me know if you're interested."

RULES
- Show, don't claim. "Wrote for Forbes and Buzzfeed" beats "experienced writer."
- Numbers and named clients beat adjectives every time. Use them, drop the adjectives.
- One CTA only. Give the client one easy next step. Do not ask multiple questions.
- Only ask a question if it demonstrates you understand the scope (timeline, deliverable, a business detail). Never ask something the post already answers.
- No hype, no long preamble, no reciting the full CV. Brief and concise wins because clients scan.

OUTPUT
Return only the finished cover letter, ready to paste. No commentary, no headers, no "Here's your proposal.", no thinking tags."""

PROPOSAL_USER_TEMPLATE = """\
Write a proposal for this Upwork job:

Title: {title}
Description: {description}
Budget: {budget}
Job Type: {job_type}
Client Country: {client_country}
Required Skills: {skills}
Project Length: {project_length}

Freelancer background: {my_role} with experience in {my_skills}."""


async def generate_proposal(
    job: dict,
    my_role: str = "freelancer",
    my_skills: str = "the required technologies",
    model: str | None = None,
    custom_system_prompt: str | None = None,
    custom_user_template: str | None = None,
    temperature: float = 0.7,
    max_tokens: int = 600,
) -> str:
    """Generate a proposal for a single job using OpenRouter.

    Supports custom templates (system_prompt + user_template) for A/B testing.
    """
    desc = job.get("description", "") or ""
    user_prompt = (custom_user_template or PROPOSAL_USER_TEMPLATE).format(
        title=job.get("title", "Untitled"),
        description=desc[:1500],
        budget=job.get("budget", "Not specified") or "Not specified",
        job_type=job.get("jobType", "Not specified") or "Not specified",
        client_country=job.get("clientCountry", "Unknown") or "Unknown",
        skills=job.get("skills", "Not specified") or "Not specified",
        project_length=job.get("projectLength", "Not specified") or "Not specified",
        my_role=my_role,
        my_skills=my_skills,
    )

    system_prompt = custom_system_prompt or PROPOSAL_SYSTEM_PROMPT

    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers=HEADERS,
            json={
                "model": model or PROPOSAL_MODEL,
                "messages": [
                    {"role": "system", "content": system_prompt},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": temperature,
                "max_tokens": max_tokens,
            },
        )
        resp.raise_for_status()
        data = resp.json()
        text = data["choices"][0]["message"]["content"]
        text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL).strip()
        return text
