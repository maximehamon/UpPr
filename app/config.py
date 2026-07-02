import os
from dotenv import load_dotenv

load_dotenv()

# ── API keys ──────────────────────────────────────────────────────────
APIFY_API_KEY = os.getenv("APIFY_API_KEY", "")
OPENROUTER_API_KEY = os.getenv("OPENROUTER_API_KEY", "")
OPENCODE_GO_API_KEY = os.getenv("OPENCODE_GO_API_KEY", "")
SLACK_WEBHOOK_URL = os.getenv("SLACK_WEBHOOK_URL", "")

# ── OpenCode Go (job scoring) ────────────────────────────────────────
OPENCODE_GO_BASE_URL = "https://opencode.ai/zen/go/v1"
SCORING_MODEL = "deepseek-v4-flash"

# ── App ───────────────────────────────────────────────────────────────
APP_BASE_URL = os.getenv("APP_BASE_URL", "http://localhost:8000")
SECRET_KEY = os.getenv("SECRET_KEY", "dev-secret-change-me")

# ── API endpoints ─────────────────────────────────────────────────────
APIFY_BASE_URL = "https://api.apify.com/v2"
OPENROUTER_BASE_URL = "https://openrouter.ai/api/v1"
ACTOR_ID = "neatrat~upwork-job-scraper"

# ── Defaults (user can override via settings) ─────────────────────────
PROPOSAL_MODEL = "openai/gpt-4o"
DEFAULT_MY_ROLE = "freelancer"
DEFAULT_MY_SKILLS = "the required technologies"

# ── Notion (override via Settings page) ────────────────────────────────
NOTION_API_KEY=os.getenv("NOTION_API_KEY", "")
NOTION_DATABASE_ID = os.getenv("NOTION_DATABASE_ID", "")
