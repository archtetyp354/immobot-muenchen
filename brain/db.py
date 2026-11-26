import logging
import os
import sqlite3

import httpx

logger = logging.getLogger("immobot-db")

DB_PATH = os.environ.get("IMMOBOT_DB", "/app/data/immobot.db")
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ENV_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
DEFAULT_PROFIL = (
    "Berufstätig in unbefristeter Festanstellung, Nichtraucher, keine Haustiere, "
    "ruhiger Mieter, alle Unterlagen (Schufa, Gehaltsnachweise) liegen komplett vor."
)

SMTP_HOST = os.environ.get("SMTP_HOST", "").strip()
SMTP_PORT = int(os.environ.get("SMTP_PORT", "587") or "587")
SMTP_USER = os.environ.get("SMTP_USER", "").strip()
SMTP_PASSWORD = os.environ.get("SMTP_PASSWORD", "").strip()
SMTP_FROM = os.environ.get("SMTP_FROM", "").strip() or SMTP_USER
IMAP_HOST = os.environ.get("IMAP_HOST", "").strip()
IMAP_PORT = int(os.environ.get("IMAP_PORT", "993") or "993")
IMAP_USER = os.environ.get("IMAP_USER", "").strip()
IMAP_PASSWORD = os.environ.get("IMAP_PASSWORD", "").strip()


def is_real_key(val: str) -> bool:
    return bool(val) and not val.startswith("HIER_")


def connect() -> sqlite3.Connection:
    parent = os.path.dirname(DB_PATH)
    if parent:
        os.makedirs(parent, exist_ok=True)
    conn = sqlite3.connect(DB_PATH, timeout=30)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    conn.execute("PRAGMA busy_timeout=5000")
    conn.execute("PRAGMA foreign_keys=ON")
    return conn


def _add_columns(conn: sqlite3.Connection, table: str, columns: list[tuple[str, str]]):
    cols = [r[1] for r in conn.execute(f"PRAGMA table_info({table})").fetchall()]
    for name, coldef in columns:
        if name not in cols:
            conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {coldef}")


def init_db():
    with connect() as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_listings (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT UNIQUE,
                title TEXT,
                url TEXT,
                lage TEXT DEFAULT '',
                preis TEXT DEFAULT '',
                feedback TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _add_columns(conn, "seen_listings", [
            ("lage", "TEXT DEFAULT ''"),
            ("preis", "TEXT DEFAULT ''"),
            ("feedback", "TEXT DEFAULT ''"),
        ])
        conn.execute("CREATE TABLE IF NOT EXISTS checked_urls (url_hash TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)")
        conn.execute("CREATE TABLE IF NOT EXISTS learned_rules (id INTEGER PRIMARY KEY AUTOINCREMENT, rule TEXT UNIQUE, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE IF NOT EXISTS negative_keywords (keyword TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE IF NOT EXISTS blacklist_urls (pattern TEXT PRIMARY KEY, reason TEXT DEFAULT '', created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE IF NOT EXISTS learned_noise (text_hash TEXT PRIMARY KEY, created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE IF NOT EXISTS domain_rules (domain TEXT PRIMARY KEY, mode TEXT, strikes INTEGER DEFAULT 0, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute("CREATE TABLE IF NOT EXISTS stealth_watches (url TEXT PRIMARY KEY, title TEXT, last_text TEXT DEFAULT '', fail_count INTEGER DEFAULT 0, updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS ignored_samples (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                source_url TEXT,
                title TEXT,
                reason TEXT,
                snippet TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS source_stats (
                source_key TEXT PRIMARY KEY,
                title TEXT DEFAULT '',
                triggers_total INTEGER DEFAULT 0,
                true_positives INTEGER DEFAULT 0,
                ignored_noise INTEGER DEFAULT 0,
                errors_count INTEGER DEFAULT 0,
                last_error TEXT DEFAULT '',
                updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_properties (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                fingerprint TEXT,
                source TEXT,
                sources TEXT DEFAULT '',
                title TEXT,
                url TEXT,
                lage TEXT DEFAULT '',
                preis TEXT DEFAULT '',
                price_eur INTEGER,
                size_m2 REAL,
                rooms REAL,
                phone_e164 TEXT DEFAULT '',
                email TEXT DEFAULT '',
                listing_id INTEGER,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )
        _add_columns(conn, "seen_properties", [
            ("sources", "TEXT DEFAULT ''"),
            ("price_eur", "INTEGER"),
            ("size_m2", "REAL"),
            ("rooms", "REAL"),
            ("phone_e164", "TEXT DEFAULT ''"),
            ("email", "TEXT DEFAULT ''"),
            ("listing_id", "INTEGER"),
        ])
        conn.execute("CREATE INDEX IF NOT EXISTS idx_seen_prop_created ON seen_properties(created_at)")
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS inquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                property_id INTEGER,
                listing_id INTEGER,
                fingerprint TEXT,
                recipient TEXT,
                subject TEXT,
                body TEXT,
                phone_e164 TEXT DEFAULT '',
                channel TEXT DEFAULT 'email',
                status TEXT DEFAULT 'scheduled',
                smtp_message_id TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
                sent_at TIMESTAMP,
                replied_at TIMESTAMP
            )
            """
        )
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS winning_inquiries (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                inquiry_id INTEGER UNIQUE,
                kind TEXT,
                note TEXT DEFAULT '',
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


def get_state(key: str, default: str = "") -> str:
    with connect() as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_state(key: str, value: str):
    with connect() as conn:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def inc_counter(key: str):
    cur = int(get_state(key, "0") or "0")
    set_state(key, str(cur + 1))


def save_ignored_sample(source_url: str, title: str, reason: str, text: str):
    import re
    snippet = re.sub(r"\s+", " ", text or "").strip()[:300]
    if len(snippet) < 25:
        return
    with connect() as conn:
        conn.execute(
            "INSERT INTO ignored_samples (source_url, title, reason, snippet) VALUES (?, ?, ?, ?)",
            (source_url[:200], title[:100], reason[:120], snippet),
        )
        conn.execute("DELETE FROM ignored_samples WHERE id NOT IN (SELECT id FROM ignored_samples ORDER BY id DESC LIMIT 100)")


def record_source_stat(source_key: str, title: str = "", trigger: int = 0, tp: int = 0, ignore: int = 0, err: int = 0, last_error: str = ""):
    if not source_key:
        return
    with connect() as conn:
        conn.execute(
            """
            INSERT INTO source_stats (source_key, title, triggers_total, true_positives, ignored_noise, errors_count, last_error)
            VALUES (?, ?, ?, ?, ?, ?, ?)
            ON CONFLICT(source_key) DO UPDATE SET
                title = COALESCE(NULLIF(excluded.title, ''), source_stats.title),
                triggers_total = source_stats.triggers_total + excluded.triggers_total,
                true_positives = source_stats.true_positives + excluded.true_positives,
                ignored_noise = source_stats.ignored_noise + excluded.ignored_noise,
                errors_count = source_stats.errors_count + excluded.errors_count,
                last_error = CASE WHEN excluded.last_error != '' THEN excluded.last_error ELSE source_stats.last_error END,
                updated_at = CURRENT_TIMESTAMP
            """,
            (source_key.strip(), title[:120], trigger, tp, ignore, err, last_error[:200]),
        )


def add_blacklist(pattern: str, reason: str = ""):
    p = pattern.strip().lower()
    if not p:
        return
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO blacklist_urls (pattern, reason) VALUES (?, ?)", (p, reason[:200]))


def is_blacklisted(url: str) -> bool:
    from urllib.parse import urlparse
    u = url.lower().strip()
    dom = urlparse(u).netloc.lower().replace("www.", "")
    with connect() as conn:
        rows = conn.execute("SELECT pattern FROM blacklist_urls").fetchall()
    for (pat,) in rows:
        if pat == u or (dom and (pat == dom or pat in u)):
            return True
    return False


def get_negative_keywords() -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT keyword FROM negative_keywords").fetchall()]


def set_consolidated_negative_keywords(keywords: list[str]):
    cleaned = [k.strip().lower() for k in keywords if k and len(k.strip()) >= 3][:40]
    with connect() as conn:
        conn.execute("DELETE FROM negative_keywords")
        for k in cleaned:
            conn.execute("INSERT OR IGNORE INTO negative_keywords (keyword) VALUES (?)", (k,))


def get_gemini_key() -> str:
    override = get_state("gemini_api_key_override", "").strip()
    return override if is_real_key(override) else ENV_GEMINI_API_KEY


def get_budget() -> int:
    try:
        return int(get_state("max_budget", "1000") or "1000")
    except ValueError:
        return 1000


def get_cd_api_key() -> str:
    return get_state("cd_api_key", "").strip()


def get_mail_mode() -> str:
    mode = get_state("mail_mode", "off").strip().lower()
    return mode if mode in ("auto", "countdown", "off") else "off"


def set_mail_mode(mode: str):
    if mode in ("auto", "countdown", "off"):
        set_state("mail_mode", mode)


def smtp_ready() -> bool:
    return bool(SMTP_HOST and SMTP_USER and SMTP_PASSWORD and SMTP_FROM)


def imap_ready() -> bool:
    return bool(IMAP_HOST and IMAP_USER and IMAP_PASSWORD)


def get_learned_rules() -> list[tuple[int, str]]:
    with connect() as conn:
        return conn.execute("SELECT id, rule FROM learned_rules ORDER BY id DESC LIMIT 15").fetchall()


def add_learned_rule(rule: str):
    rule = rule.strip()
    if not rule:
        return
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO learned_rules (rule) VALUES (?)", (rule,))


def delete_learned_rule(rule_id: int):
    with connect() as conn:
        conn.execute("DELETE FROM learned_rules WHERE id = ?", (rule_id,))


def set_consolidated_rules(rules: list[str]):
    cleaned = [r.strip() for r in rules if r and len(r.strip()) >= 4][:10]
    with connect() as conn:
        conn.execute("DELETE FROM learned_rules")
        for r in cleaned:
            conn.execute("INSERT OR IGNORE INTO learned_rules (rule) VALUES (?)", (r,))


def is_learned_noise(clean_diff: str) -> bool:
    import hashlib
    h = hashlib.sha256(clean_diff[:1000].lower().encode("utf-8")).hexdigest()
    with connect() as conn:
        return conn.execute("SELECT 1 FROM learned_noise WHERE text_hash = ?", (h,)).fetchone() is not None


def remember_noise(clean_diff: str):
    import hashlib
    if len(clean_diff) < 25:
        return
    h = hashlib.sha256(clean_diff[:1000].lower().encode("utf-8")).hexdigest()
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO learned_noise (text_hash) VALUES (?)", (h,))


def load_bewerber_profil() -> str:
    if os.path.exists("/app/profil.txt"):
        with open("/app/profil.txt", "r", encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text
    return os.environ.get("BEWERBER_PROFIL", DEFAULT_PROFIL).strip()


def is_already_seen(fingerprint: str) -> bool:
    with connect() as conn:
        return conn.execute("SELECT 1 FROM seen_listings WHERE fingerprint = ?", (fingerprint,)).fetchone() is not None


def save_seen_listing(fingerprint: str, title: str, url: str, lage: str = "", preis: str = "") -> int:
    with connect() as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO seen_listings (fingerprint, title, url, lage, preis) VALUES (?, ?, ?, ?, ?)",
            (fingerprint, title, url, lage, preis),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute("SELECT id FROM seen_listings WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return row[0] if row else 0


def get_listing(listing_id: int) -> tuple | None:
    with connect() as conn:
        return conn.execute(
            "SELECT title, url, lage, preis FROM seen_listings WHERE id = ?",
            (listing_id,),
        ).fetchone()


def set_listing_feedback(listing_id: int, kind: str):
    with connect() as conn:
        conn.execute("UPDATE seen_listings SET feedback = ? WHERE id = ?", (kind, listing_id))


def is_url_checked(url: str) -> bool:
    import hashlib
    if not url:
        return False
    h = hashlib.sha256(url.lower().strip().encode("utf-8")).hexdigest()
    with connect() as conn:
        return conn.execute("SELECT 1 FROM checked_urls WHERE url_hash = ?", (h,)).fetchone() is not None


def mark_url_checked(url: str):
    import hashlib
    if not url:
        return
    h = hashlib.sha256(url.lower().strip().encode("utf-8")).hexdigest()
    with connect() as conn:
        conn.execute("INSERT OR IGNORE INTO checked_urls (url_hash) VALUES (?)", (h,))


def set_domain_rule(domain: str, mode: str, strikes: int = 0):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO domain_rules (domain, mode, strikes, updated_at) VALUES (?, ?, ?, CURRENT_TIMESTAMP)",
            (domain, mode, strikes),
        )


def bump_domain_strikes(domain: str) -> int:
    with connect() as conn:
        row = conn.execute("SELECT strikes FROM domain_rules WHERE domain = ?", (domain,)).fetchone()
        strikes = (row[0] if row else 0) + 1
        conn.execute(
            "INSERT OR REPLACE INTO domain_rules (domain, mode, strikes, updated_at) VALUES (?, 'blocked', ?, CURRENT_TIMESTAMP)",
            (domain, strikes),
        )
        return strikes


def chrome_domains() -> set[str]:
    with connect() as conn:
        return {r[0] for r in conn.execute("SELECT domain FROM domain_rules WHERE mode = 'chrome'").fetchall()}


def upsert_stealth(url: str, title: str, text: str):
    with connect() as conn:
        conn.execute(
            "INSERT OR REPLACE INTO stealth_watches (url, title, last_text, fail_count) VALUES (?, ?, ?, 0)",
            (url, title, text),
        )


def stealth_urls() -> list[str]:
    with connect() as conn:
        return [r[0] for r in conn.execute("SELECT url FROM stealth_watches").fetchall()]


def list_stealth() -> list[tuple]:
    with connect() as conn:
        return conn.execute("SELECT url, title, last_text, fail_count FROM stealth_watches").fetchall()


def update_stealth_text(url: str, text: str):
    with connect() as conn:
        conn.execute(
            "UPDATE stealth_watches SET last_text = ?, fail_count = 0, updated_at = CURRENT_TIMESTAMP WHERE url = ?",
            (text, url),
        )


def bump_stealth_fail(url: str, fails: int):
    with connect() as conn:
        conn.execute("UPDATE stealth_watches SET fail_count = ? WHERE url = ?", (fails, url))


def status_counts() -> dict:
    with connect() as conn:
        return {
            "hits": conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0],
            "stealth": conn.execute("SELECT COUNT(*) FROM stealth_watches").fetchone()[0],
            "ignored": conn.execute("SELECT COUNT(*) FROM ignored_samples").fetchone()[0],
            "rules": conn.execute("SELECT COUNT(*) FROM learned_rules").fetchone()[0],
        }


def recent_listings(limit: int = 20) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT title, url, created_at FROM seen_listings ORDER BY created_at DESC LIMIT ?",
            (limit,),
        ).fetchall()


def top_sources(limit: int = 12) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            "SELECT title, source_key, triggers_total, true_positives, ignored_noise, errors_count FROM source_stats ORDER BY triggers_total DESC LIMIT ?",
            (limit,),
        ).fetchall()


def agent_rows() -> dict:
    with connect() as conn:
        return {
            "stats": conn.execute(
                "SELECT source_key, title, triggers_total, true_positives, ignored_noise, errors_count, last_error FROM source_stats ORDER BY triggers_total DESC LIMIT 40"
            ).fetchall(),
            "feedback": conn.execute(
                "SELECT title, url, lage, preis, feedback FROM seen_listings WHERE feedback != '' ORDER BY id DESC LIMIT 15"
            ).fetchall(),
            "ignored": conn.execute(
                "SELECT source_url, title, reason, snippet FROM ignored_samples ORDER BY id DESC LIMIT 10"
            ).fetchall(),
            "blacklist": conn.execute("SELECT pattern, reason FROM blacklist_urls").fetchall(),
            "rules": conn.execute("SELECT rule FROM learned_rules ORDER BY id DESC").fetchall(),
            "keywords": conn.execute("SELECT keyword FROM negative_keywords").fetchall(),
        }


def recent_properties(within_days: int = 14) -> list[tuple]:
    with connect() as conn:
        return conn.execute(
            """
            SELECT id, fingerprint, source, sources, title, url, lage, preis, price_eur, size_m2, rooms, phone_e164, email
            FROM seen_properties
            WHERE created_at >= datetime('now', ?)
            ORDER BY id DESC
            LIMIT 400
            """,
            (f"-{int(within_days)} days",),
        ).fetchall()


def insert_property(row: dict) -> int:
    with connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO seen_properties (
                fingerprint, source, sources, title, url, lage, preis, price_eur, size_m2, rooms, phone_e164, email, listing_id
            ) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (
                row.get("fingerprint", ""),
                row.get("source", ""),
                row.get("sources", row.get("source", "")),
                row.get("title", ""),
                row.get("url", ""),
                row.get("lage", ""),
                row.get("preis", ""),
                row.get("price_eur"),
                row.get("size_m2"),
                row.get("rooms"),
                row.get("phone_e164", ""),
                row.get("email", ""),
                row.get("listing_id"),
            ),
        )
        return cur.lastrowid or 0


def note_property_source(property_id: int, source: str):
    with connect() as conn:
        row = conn.execute("SELECT sources FROM seen_properties WHERE id = ?", (property_id,)).fetchone()
        if not row:
            return
        parts = [p for p in (row[0] or "").split(",") if p]
        if source not in parts:
            parts.append(source)
        conn.execute("UPDATE seen_properties SET sources = ? WHERE id = ?", (",".join(parts), property_id))


def attach_listing(property_id: int, listing_id: int):
    with connect() as conn:
        conn.execute("UPDATE seen_properties SET listing_id = ? WHERE id = ?", (listing_id, property_id))


async def send_telegram(text_html: str, reply_markup: dict | None = None) -> tuple[bool, str]:
    if not is_real_key(TELEGRAM_TOKEN) or not is_real_key(TELEGRAM_CHAT_ID):
        return False, "TELEGRAM_TOKEN oder TELEGRAM_CHAT_ID fehlt in .env"
    if len(text_html) > 4000:
        text_html = text_html[:3990] + "\n..."
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": text_html,
        "parse_mode": "HTML",
        "disable_web_page_preview": False,
    }
    if reply_markup:
        payload["reply_markup"] = reply_markup
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(url, json=payload)
        if resp.status_code != 200:
            logger.error("Telegram Fehler: %s %s", resp.status_code, resp.text)
            return False, f"Telegram {resp.status_code}: {resp.text}"
        return True, "ok"


async def answer_callback(callback_id: str, text: str):
    if not callback_id or not is_real_key(TELEGRAM_TOKEN):
        return
    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": callback_id, "text": text[:180]},
        )


try:
    init_db()
except OSError as exc:
    logger.warning("SQLite-Init übersprungen: %s", exc)
