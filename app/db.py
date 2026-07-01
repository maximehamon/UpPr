import aiosqlite
import os
from contextlib import asynccontextmanager

DB_PATH = os.environ.get("RENDER_DISK_PATH") or os.environ.get("FLY_DATA_PATH", ".")
if DB_PATH and not DB_PATH.endswith("/"):
    DB_PATH = DB_PATH + "/"
DB_PATH = DB_PATH + "data.db"

_tables_created = False


@asynccontextmanager
async def get_db():
    global _tables_created
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    if not _tables_created:
        await _ensure_tables(db)
        _tables_created = True
    try:
        yield db
    finally:
        await db.close()


async def _ensure_tables(db: aiosqlite.Connection):
    """Idempotent table creation + migration — runs once at startup."""
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
            new_count INTEGER DEFAULT 0,
            results_json TEXT,
            error_message TEXT DEFAULT ''
        );
        CREATE TABLE IF NOT EXISTS seen_jobs (
            url TEXT PRIMARY KEY,
            first_seen_at TEXT NOT NULL DEFAULT (datetime('now'))
        );
        CREATE TABLE IF NOT EXISTS proposals (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            created_at TEXT NOT NULL DEFAULT (datetime('now')),
            scrape_id INTEGER NOT NULL,
            job_index INTEGER DEFAULT 0,
            job_data TEXT NOT NULL,
            status TEXT DEFAULT 'draft',
            proposal_text TEXT,
            model_used TEXT,
            template_id TEXT,
            FOREIGN KEY (scrape_id) REFERENCES scrapes(id)
        );
        CREATE TABLE IF NOT EXISTS settings (
            key TEXT PRIMARY KEY,
            value TEXT NOT NULL
        );
        INSERT OR IGNORE INTO settings (key, value) VALUES
            ('my_role', 'freelancer'),
            ('my_skills', ''),
            ('model', 'openai/gpt-4o'),
            ('slack_webhook_url', '');
    """)

    try:
        cursor = await db.execute("PRAGMA table_info(proposals)")
        columns = [row["name"] for row in await cursor.fetchall()]
        if "template_id" not in columns:
            await db.execute("ALTER TABLE proposals ADD COLUMN template_id TEXT")
            await db.commit()
    except Exception:
        pass


async def init_db():
    """Create all tables on startup."""
    async with get_db():
        pass


async def get_setting(key: str, default: str = "") -> str:
    async with get_db() as db:
        row = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
        result = await row.fetchone()
        return result["value"] if result else default


async def get_settings_bulk(keys: dict[str, str]) -> dict[str, str]:
    """Fetch multiple settings in one connection. keys = {key: default_value}."""
    async with get_db() as db:
        placeholders = ",".join("?" for _ in keys)
        rows = await db.execute(
            f"SELECT key, value FROM settings WHERE key IN ({placeholders})",
            list(keys.keys()),
        )
        found = {r["key"]: r["value"] for r in await rows.fetchall()}
        return {k: found.get(k, default) for k, default in keys.items()}


async def set_setting(key: str, value: str):
    async with get_db() as db:
        await db.execute(
            "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
            (key, value),
        )
        await db.commit()


async def set_settings_bulk(settings: dict[str, str]):
    """Write multiple settings in one connection."""
    async with get_db() as db:
        for key, value in settings.items():
            await db.execute(
                "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
                (key, value),
            )
        await db.commit()
