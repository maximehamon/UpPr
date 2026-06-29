# Upwork Job Scraper → Proposal Pipeline Web App

> **For Hermes:** Use subagent-driven-development skill to implement this plan task-by-task.

**Goal:** A deployable web app that (1) triggers the Apify `neatrat/upwork-job-scraper` actor with user-specified keywords, (2) displays results and sends notifications, and (3) drafts Upwork proposals via OpenRouter LLM when the user approves a job.

**Architecture:** Python FastAPI backend + vanilla HTML/JS frontend + SQLite for persistence. The backend wraps the Apify REST API (run actor → poll for completion → fetch dataset) and the OpenRouter chat completions API. The frontend is a single-page app served from the same FastAPI process — no separate frontend build step.

**Tech Stack:** FastAPI, Jinja2 templates, SQLite (aiosqlite), httpx (async HTTP), OpenRouter API, Apify API v2

**Deployment targets:** Render, Railway, or Fly.io — all support Python/FastAPI with minimal config.

---

## External APIs — Key Facts

### Apify API v2 (neatrat/upwork-job-scraper)

- **Base:** `https://api.apify.com/v2`
- **Auth:** `Authorization: Bearer $APIFY_API_KEY`
- **Actor ID:** `neatrat~upwork-job-scraper` (tilde, not slash)
- **Run actor:** `POST /acts/neatrat~upwork-job-scraper/runs` with JSON body containing input
- **Get run status:** `GET /acts/neatrat~upwork-job-scraper/runs/{runId}`
- **Fetch dataset items:** `GET /acts/neatrat~upwork-job-scraper/runs/{runId}/dataset/items`
- **Input schema** (from actor page):
  - `searchTerms`: list of search query strings (e.g. `["react developer", "python scraping"]`)
  - `maxJobs` (optional): max number of jobs to scrape (default ~50)
  - `jobType` (optional): `"hourly"` or `"fixed"`
  - Pricing: PAY_PER_EVENT, ~$0.0032–$0.0035 per job (~$3.20/1,000 jobs)

### OpenRouter API

- **Base:** `https://openrouter.ai/api/v1`
- **Auth:** `Authorization: Bearer $OPENROUTER_API_KEY`
- **Chat completions:** `POST /chat/completions` — OpenAI-compatible schema
- **Headers:** `HTTP-Referer` (your site URL) and `X-Title` (your app name) are required for ranking

---

## Step-by-Step Plan

### Task 1: Project scaffolding

**Objective:** Create the project directory structure, dependencies, and config skeleton.

**Files:**
- Create: `upwork-pipeline/pyproject.toml`
- Create: `upwork-pipeline/.env.example`
- Create: `upwork-pipeline/.gitignore`
- Create: `upwork-pipeline/app/__init__.py`
- Create: `upwork-pipeline/app/main.py` (minimal FastAPI app)
- Create: `upwork-pipeline/app/config.py`

**Step 1: Create pyproject.toml**

```toml
[project]
name = "upwork-pipeline"
version = "0.1.0"
requires-python = ">=3.11"
dependencies = [
    "fastapi",
    "uvicorn[standard]",
    "jinja2",
    "httpx",
    "python-dotenv",
    "aiosqlite",
]

[build-system]
requires = ["hatchling"]
build-backend = "hatchling.build"
```

**Step 2: Create .env.example**

```
APIFY_API_KEY=apify_your_key_here
OPENROUTER_API_KEY=sk-or-v1-your_key_here
APP_BASE_URL=http://localhost:8000
SECRET_KEY=replace-with-random-string
```

**Step 3: Create config.py**

```python
import os
from dotenv import load_dotenv

load_dotenv()

APIFY_API_KEY = os.getenv("APIFY_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret")

APIFY_BASE_URL = "https://api.apify.com/v2"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
ACTOR_ID = "neatrat~upwork-job-scraper"
PROPOSAL_MODEL = "openai/gpt-4o"
```

**Step 4: Create minimal main.py**

```python
from fastapi import FastAPI

app = FastAPI(title="Upwork Proposal Pipeline")

@app.get("/")
async def root():
    return {"status": "ok", "app": "Upwork Proposal Pipeline"}
```

**Step 5: Install and verify**

Run: `cd upwork-pipeline && uv sync`
Run: `uv run uvicorn app.main:app --port 8000`
Verify: `curl http://localhost:8000/` returns `{"status": "ok"}`

**Step 6: Commit**

```bash
git init && git add . && git commit -m "feat: project scaffolding with FastAPI + config"
```

---

### Task 2: Database layer (SQLite)

**Objective:** Set up aiosqlite database for persisting scrape jobs, results, and proposals.

**Files:**
- Create: `upwork-pipeline/app/db.py`

**Step 1: Write db.py with schema and helper functions**

```python
import aiosqlite

DB_PATH = "data.db"

async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    return db

async def init_db():
    db = await get_db()
    await db.executescript("""
        CREATE TABLE IF NOT EXISTS scrapes (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            keywords TEXT NOT NULL,
            max_jobs INTEGER DEFAULT 50,
            job_type TEXT DEFAULT 'hourly',
            status TEXT DEFAULT 'pending',
            apify_run_id TEXT,
            result_count INTEGER DEFAULT 0,
            results_json TEXT
        );
        CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            scrape_id INTEGER NOT NULL,
            job_data TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            proposal_text TEXT,
            model_used TEXT,
            FOREIGN KEY (scrape_id) REFERENCES scrapes(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
    """)
    await db.commit()
    await db.close()
```

**Step 2: Add startup event to main.py**

```python
@app.on_event("startup")
async def startup():
    from app.db import init_db
    await init_db()
```

**Step 3: Verify**

Run: `cd upwork-pipeline && rm -f data.db && uv run python -c "import asyncio; from app.db import init_db; asyncio.run(init_db()); print('OK')"`

**Step 4: Commit**

```bash
git add . && git commit -m "feat: SQLite database layer with scrapes/proposals/settings tables"
```

---

### Task 3: Apify actor integration

**Objective:** Module that triggers the Apify actor, polls for completion, and fetches results.

**Files:**
- Create: `upwork-pipeline/app/apify_client.py`

**Step 1: Write apify_client.py**

```python
import httpx
import asyncio
from app.config import APIFY_API_KEY, APIFY_BASE_URL, ACTOR_ID

HEADERS = {"Authorization": f"Bearer {APIFY_API_KEY}", "Content-Type": "application/json"}

async def start_scrape(keywords: list[str], max_jobs: int = 50, job_type: str = "hourly") -> dict:
    input_data = {"searchTerms": keywords, "maxJobs": max_jobs, "jobType": job_type}
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.post(
            f"{APIFY_BASE_URL}/acts/{ACTOR_ID}/runs",
            headers=HEADERS, json=input_data)
        resp.raise_for_status()
        data = resp.json()["data"]
        return {"run_id": data["id"], "status": data["status"]}

async def get_run_status(run_id: str) -> dict:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{APIFY_BASE_URL}/acts/{ACTOR_ID}/runs/{run_id}", headers=HEADERS)
        resp.raise_for_status()
        data = resp.json()["data"]
        return {"run_id": data["id"], "status": data["status"], "finished_at": data.get("finishedAt")}

async def fetch_dataset(run_id: str) -> list[dict]:
    async with httpx.AsyncClient(timeout=30) as client:
        resp = await client.get(
            f"{APIFY_BASE_URL}/acts/{ACTOR_ID}/runs/{run_id}/dataset/items", headers=HEADERS)
        resp.raise_for_status()
        return resp.json()

async def run_and_wait(keywords: list[str], max_jobs: int = 50, job_type: str = "hourly",
                       poll_interval: int = 5, max_wait: int = 300) -> list[dict]:
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
```

**Step 2: Commit**

```bash
git add . && git commit -m "feat: Apify actor integration — start, poll, fetch results"
```

---

### Task 4: OpenRouter proposal generator

**Objective:** Module that takes a job listing and generates a tailored proposal using OpenRouter.

**Files:**
- Create: `upwork-pipeline/app/proposal_writer.py`

**Step 1: Write proposal_writer.py**

```python
import httpx
from app.config import OPENROUTER_API_KEY, OPENROUTER_BASE_URL, PROPOSAL_MODEL, APP_BASE_URL

HEADERS = {
    "Authorization": f"Bearer {OPENROUTER_API_KEY}",
    "Content-Type": "application/json",
    "HTTP-Referer": APP_BASE_URL,
    "X-Title": "Upwork Proposal Pipeline",
}

PROPOSAL_SYSTEM_PROMPT = """You are an expert Upwork proposal writer. Given a job listing, write a compelling,
personalized proposal that:
1. Opens with a specific reference to the client's project
2. Demonstrates relevant experience
3. Asks a thoughtful question about the project
4. Includes a clear call to action

Keep it concise (150-250 words), professional, and warm. Do NOT use generic templates.
Format the output as plain text (no markdown)."""

PROPOSAL_USER_TEMPLATE = """Write a proposal for this Upwork job:

Title: {title}
Description: {description}
Budget: {budget}
Job Type: {job_type}
Client Country: {client_country}
Required Skills: {skills}

My background: I am a skilled {my_role} with experience in {my_skills}.
I deliver high-quality work on time and communicate clearly."""

async def generate_proposal(job: dict, my_role: str = "freelancer",
                            my_skills: str = "the required technologies",
                            model: str = None) -> str:
    user_prompt = PROPOSAL_USER_TEMPLATE.format(
        title=job.get("title", "Untitled"),
        description=job.get("description", "")[:1500],
        budget=job.get("budget", "Not specified"),
        job_type=job.get("jobType", "Not specified"),
        client_country=job.get("clientCountry", "Unknown"),
        skills=job.get("skills", "Not specified"),
        my_role=my_role, my_skills=my_skills,
    )
    async with httpx.AsyncClient(timeout=60) as client:
        resp = await client.post(
            f"{OPENROUTER_BASE_URL}/chat/completions",
            headers=HEADERS,
            json={
                "model": model or PROPOSAL_MODEL,
                "messages": [
                    {"role": "system", "content": PROPOSAL_SYSTEM_PROMPT},
                    {"role": "user", "content": user_prompt},
                ],
                "temperature": 0.7,
                "max_tokens": 600,
            },
        )
        resp.raise_for_status()
        return resp.json()["choices"][0]["message"]["content"]
```

**Step 2: Commit**

```bash
git add . && git commit -m "feat: OpenRouter proposal generator module"
```

---

### Task 5: API routes

**Objective:** Create all FastAPI routes — scrape trigger, status check, result listing, proposal generation.

**Files:**
- Create: `upwork-pipeline/app/routes.py`
- Modify: `upwork-pipeline/app/main.py`

**Step 1: Write routes.py**

All routes: `POST /api/scrapes`, `GET /api/scrapes`, `GET /api/scrapes/{id}`, `POST /api/scrapes/{id}/refresh`, `POST /api/proposals`, `GET /api/proposals`, `GET /api/proposals/{id}`, `DELETE /api/proposals/{id}`, `GET /api/settings`, `POST /api/settings`.

**Step 2: Register in main.py**

```python
from app.routes import router as api_router
app.include_router(api_router, prefix="/api")
```

**Step 3: Commit**

```bash
git add . && git commit -m "feat: all API routes — scrapes CRUD, proposal generation, settings"
```

---

### Task 6: Frontend — dashboard + scrape form + results + proposals

**Objective:** Create the Jinja2 templates and static assets for the web UI.

**Files:**
- Create: `upwork-pipeline/app/templates/base.html`
- Create: `upwork-pipeline/app/templates/index.html`
- Create: `upwork-pipeline/app/templates/proposals.html`
- Create: `upwork-pipeline/app/templates/settings.html`
- Create: `upwork-pipeline/app/static/style.css`
- Modify: `upwork-pipeline/app/main.py` (mount static, add page routes)

**Step 1: base.html** — responsive layout, nav with links to /, /proposals, /settings
**Step 2: index.html** — keyword input form + scrape history table + results panel + "Generate Proposal" button per job
**Step 3: proposals.html** — list of saved proposals, click to expand full text
**Step 4: settings.html** — form for API keys, model, my_role, my_skills, notification webhook URL
**Step 5: style.css** — dark theme, clean modern design
**Step 6: Mount static in main.py**

```python
from fastapi.staticfiles import StaticFiles
app.mount("/static", StaticFiles(directory="app/static"), name="static")
```

**Step 7: Verify** — visit `http://localhost:8000`, see dashboard

**Step 8: Commit**

```bash
git add . && git commit -m "feat: frontend — dashboard, scrape form, results, proposals, settings"
```

---

### Task 7: Background polling + notification

**Objective:** Auto-poll Apify in the background and optionally POST results to a webhook.

**Files:**
- Create: `upwork-pipeline/app/poller.py`
- Modify: `upwork-pipeline/app/routes.py`

**Step 1: poller.py** — `async def poll_scrape(scrape_id, run_id)` that polls every 10s, updates DB status + results, fires webhook if set
**Step 2: In routes.py POST /api/scrapes**, call `asyncio.create_task(poll_scrape(...))` after creating DB row

**Step 3: Commit**

```bash
git add . && git commit -m "feat: background Apify polling with webhook notification support"
```

---

### Task 8: Deployment configs

**Objective:** Add production deployment configs for Render, Railway, and Docker.

**Files:**
- Create: `upwork-pipeline/render.yaml`
- Create: `upwork-pipeline/Dockerfile`
- Create: `upwork-pipeline/Procfile`

**Step 1: render.yaml**

```yaml
services:
  - type: web
    name: upwork-pipeline
    env: python
    buildCommand: pip install .
    startCommand: uvicorn app.main:app --host 0.0.0.0 --port $PORT
    envVars:
      - key: APIFY_API_KEY
        sync: false
      - key: OPENROUTER_API_KEY
        sync: false
      - key: SECRET_KEY
        generateValue: true
```

**Step 2: Dockerfile** — python:3.11-slim, install from pyproject.toml, copy app/, run uvicorn
**Step 3: Procfile** — `web: uvicorn app.main:app --host 0.0.0.0 --port $PORT`

**Step 4: Commit**

```bash
git add . && git commit -m "chore: deployment configs for Render, Railway, Docker"
```

---

### Task 9: README

**Objective:** Write a comprehensive README.

**Files:**
- Create: `upwork-pipeline/README.md`

**Step 1: README.md** covering:
- What it does (screenshot placeholder)
- Prerequisites (Python 3.11+, Apify account, OpenRouter account)
- Quick start (clone, uv sync, cp .env.example .env, fill keys, uv run uvicorn ...)
- Deploy to Render/Railway (point at the yaml)
- How it works (architecture diagram in text)
- Cost estimates

**Step 2: Commit**

```bash
git add . && git commit -m "docs: README with setup and deployment instructions"
```

---

### Task 10: Final integration test

**Objective:** Full end-to-end test locally.

**Steps:**
1. Start the app: `uv run uvicorn app.main:app --port 8000`
2. Open browser at `http://localhost:8000`
3. Enter keywords like `["python developer", "web scraping"]`
4. Start a scrape
5. Wait for results to appear (Apify polling)
6. Click "Generate Proposal" on a result
7. Verify LLM-generated proposal appears
8. Check settings page loads
9. Done

---

## Files Summary

| File | Purpose |
|------|---------|
| `pyproject.toml` | Python project config + dependencies |
| `.env.example` | Environment variable template |
| `.gitignore` | Python gitignore |
| `app/main.py` | FastAPI app entry point |
| `app/config.py` | Configuration from env vars |
| `app/db.py` | SQLite database layer (aiosqlite) |
| `app/apify_client.py` | Apify actor integration |
| `app/proposal_writer.py` | OpenRouter proposal generator |
| `app/routes.py` | All API endpoints |
| `app/poller.py` | Background Apify polling |
| `app/templates/base.html` | Base Jinja2 layout |
| `app/templates/index.html` | Dashboard + scrape form + results |
| `app/templates/proposals.html` | Proposal list + view |
| `app/templates/settings.html` | Settings page |
| `app/static/style.css` | Stylesheet |
| `Dockerfile` | Container build |
| `render.yaml` | Render deployment |
| `Procfile` | Process runner (Railway/Heroku) |
| `README.md` | Setup + deploy docs |

---

## Risks & Open Questions

1. **Apify input schema** — Exact field names (`searchTerms`, `maxJobs`, `jobType`) need confirmation against the actor's live input. If they differ, the Apify run may fail silently. The async client surfaces API errors.
2. **Polling timeout** — The actor can take several minutes. Background polling with a generous timeout (10 min) handles this.
3. **Cost tracking** — ~$0.0032/job. UI should show estimate before starting. Add in polish task.
4. **OpenRouter model availability** — Default `openai/gpt-4o` might change. Settings page lets user pick any model.
5. **No auth** — V1 has no login. Add simple API-key auth if multi-user needed.
