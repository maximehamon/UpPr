import httpx
from app.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, PROPOSAL_MODEL, APP_BASE_URL

HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": APP_BASE_URL,
    "X-Title": "Upwork Proposal Pipeline",
}

# Default template (fallback when no templates configured)
PROPOSAL_SYSTEM_PROMPT = """You are an expert Upwork proposal writer. Given a job listing, write a compelling,
personalized proposal that:
1. Opens with a specific reference to the client's project
2. Demonstrates relevant experience
3. Asks a thoughtful question about the project
4. Includes a clear call to action

Keep it concise (150-250 words), professional, and warm. Do NOT use generic templates —
each proposal must be tailored to the specific job. Format the output as plain text (no markdown).
Sign off with a professional closing."""

PROPOSAL_USER_TEMPLATE = """Write a proposal for this Upwork job:

Title: {title}
Description: {description}
Budget: {budget}
Job Type: {job_type}
Client Country: {client_country}
Required Skills: {skills}
Project Length: {project_length}

My background: I am a skilled {my_role} with experience in {my_skills}.
I deliver high-quality work on time and communicate clearly throughout the project."""


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
        return data["choices"][0]["message"]["content"]
