import aiosqlite

DB_PATH = "data.db"


async def get_db() -> aiosqlite.Connection:
    db = await aiosqlite.connect(DB_PATH)
    db.row_factory = aiosqlite.Row
    await db.execute("PRAGMA journal_mode=WAL")
    await db.execute("PRAGMA busy_timeout=5000")
    # Ensure tables exist (idempotent, fast when already created)
    await _ensure_tables(db)
    return db


async def _ensure_tables(db: aiosqlite.Connection):
    """Idempotent table creation — safe to call on every connection."""
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
            job_index INTEGER DEFAULT 0,
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
        INSERT OR IGNORE INTO settings (key, value) VALUES
            ('my_role', 'freelancer'),
            ('my_skills', ''),
            ('model', 'openai/gpt-4o'),
            ('slack_webhook_url', '');
    """)


async def init_db():
    """Create all tables on startup (now just calls get_db to ensure tables)."""
    db = await get_db()
    await db.close()


async def get_setting(key: str, default: str = "") -> str:
    db = await get_db()
    row = await db.execute("SELECT value FROM settings WHERE key = ?", (key,))
    result = await row.fetchone()
    await db.close()
    return result["value"] if result else default


async def set_setting(key: str, value: str):
    db = await get_db()
    await db.execute(
        "INSERT OR REPLACE INTO settings (key, value) VALUES (?, ?)",
        (key, value),
    )
    await db.commit()
    await db.close()
