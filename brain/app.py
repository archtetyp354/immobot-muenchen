import asyncio
from contextlib import asynccontextmanager
import hashlib
import html
import json
import logging
import os
import re
import sqlite3
import time
from urllib.parse import urlparse
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession as CffiSession
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import httpx
from google import genai
from google.genai import types
import scout

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("immobot-brain")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
ENV_GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
DEFAULT_PROFIL = "Berufstätig in unbefristeter Festanstellung, Nichtraucher, keine Haustiere, ruhiger Mieter, alle Unterlagen (Schufa, Gehaltsnachweise) liegen komplett vor."

DB_PATH = "/app/data/immobot.db"
DOCKER_SOCK = "/var/run/docker.sock"
CD_API_BASE = "http://changedetection:5000/api/v1"
os.makedirs("/app/data", exist_ok=True)

CONTAINER_ALIASES = {
    "chrome": "changedetection-chrome",
    "playwright": "changedetection-chrome",
    "changedetection": "changedetection",
    "5050": "changedetection",
    "cd": "changedetection",
    "fredy": "fredy",
    "9998": "fredy",
    "brain": "immobot-brain",
    "8000": "immobot-brain",
}

JS_REQUIRED_DOMAINS = (
    "rohrer-firmengruppe.de",
    "lpe-immobilien.com",
    "hv-gruber.de",
    "schlaugk.de",
    "esw.de",
    "aigner-immobilien.de",
    "wsb-bayern.de",
)

IMMOBILIEN_SIGNAL_RE = re.compile(
    r"(wohnung|whg|zimmer|zi\.|raum|m²|qm|quadratmeter|€|eur|miete|kalt|warm|nebenkosten|kaution|etage|balkon|altbau|neubau|vermietung|expos[eé]|objekt|angebot|apartment|studio|frei\s+ab|bezugsfrei|einbauküche)",
    re.IGNORECASE,
)

COOKIE_NOISE_MARKERS = [
    "essenzielle cookies",
    "alle cookies akzeptieren",
    "mit diesem button wird der dialog geschlossen",
    "cookie-einstellungen",
    "datenschutzeinstellungen",
    "datenschutzpräferenzen",
    "cookies akzeptieren",
    "einwilligung verwalten",
]

EXCLUDED_DISCOVERY_DOMAINS = (
    "immobilienscout24", "immowelt", "kleinanzeigen", "wg-gesucht", "immonet",
    "ebay", "facebook", "instagram", "youtube", "wikipedia", "gelbeseiten",
    "dasoertliche", "11880", "google.", "bing.", "kununu", "stepstone",
    "brf-muenchen.de", "meinestadt", "mitula", "trovit", "nestoria",
)

MODELS_FALLBACK = [
    "gemini-4-hydrogen",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.1-flash-lite",
]

DEFAULT_DISCOVERY_QUERIES = [
    "Münchner Wohnungsstiftung Mietangebote München",
    "Hausverwaltung München Sendling Westend Neuhausen Mietwohnungen",
    "Wohnungsbaugenossenschaft München freie Wohnungen Vermietung",
    "kirchliche Stiftung Pfründestiftung Wohnungen mieten München",
]


def is_real_key(val: str) -> bool:
    return bool(val) and not val.startswith("HIER_")


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
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
        cols = [r[1] for r in conn.execute("PRAGMA table_info(seen_listings)").fetchall()]
        for col, coldef in [("lage", "TEXT DEFAULT ''"), ("preis", "TEXT DEFAULT ''"), ("feedback", "TEXT DEFAULT ''")]:
            if col not in cols:
                conn.execute(f"ALTER TABLE seen_listings ADD COLUMN {col} {coldef}")

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


def get_state(key: str, default: str = "") -> str:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute("SELECT value FROM bot_state WHERE key = ?", (key,)).fetchone()
        return row[0] if row else default


def set_state(key: str, value: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO bot_state (key, value) VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (key, value),
        )


def inc_counter(key: str):
    cur = int(get_state(key, "0") or "0")
    set_state(key, str(cur + 1))


def save_ignored_sample(source_url: str, title: str, reason: str, text: str):
    snippet = re.sub(r"\s+", " ", text or "").strip()[:300]
    if len(snippet) < 25:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT INTO ignored_samples (source_url, title, reason, snippet) VALUES (?, ?, ?, ?)",
            (source_url[:200], title[:100], reason[:120], snippet),
        )
        conn.execute("DELETE FROM ignored_samples WHERE id NOT IN (SELECT id FROM ignored_samples ORDER BY id DESC LIMIT 100)")


def record_source_stat(source_key: str, title: str = "", trigger: int = 0, tp: int = 0, ignore: int = 0, err: int = 0, last_error: str = ""):
    if not source_key:
        return
    with sqlite3.connect(DB_PATH) as conn:
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
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO blacklist_urls (pattern, reason) VALUES (?, ?)", (p, reason[:200]))


def is_blacklisted(url: str) -> bool:
    u = url.lower().strip()
    dom = urlparse(u).netloc.lower().replace("www.", "")
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT pattern FROM blacklist_urls").fetchall()
    for (pat,) in rows:
        if pat == u or (dom and (pat == dom or pat in u)):
            return True
    return False


def get_negative_keywords() -> list[str]:
    with sqlite3.connect(DB_PATH) as conn:
        return [r[0] for r in conn.execute("SELECT keyword FROM negative_keywords").fetchall()]


def set_consolidated_negative_keywords(keywords: list[str]):
    cleaned = [k.strip().lower() for k in keywords if k and len(k.strip()) >= 3][:40]
    with sqlite3.connect(DB_PATH) as conn:
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


def get_learned_rules() -> list[tuple[int, str]]:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT id, rule FROM learned_rules ORDER BY id DESC LIMIT 15").fetchall()


def add_learned_rule(rule: str):
    rule = rule.strip()
    if not rule:
        return
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO learned_rules (rule) VALUES (?)", (rule,))


def set_consolidated_rules(rules: list[str]):
    cleaned = [r.strip() for r in rules if r and len(r.strip()) >= 4][:10]
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("DELETE FROM learned_rules")
        for r in cleaned:
            conn.execute("INSERT OR IGNORE INTO learned_rules (rule) VALUES (?)", (r,))


def is_learned_noise(clean_diff: str) -> bool:
    h = hashlib.sha256(clean_diff[:1000].lower().encode("utf-8")).hexdigest()
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT 1 FROM learned_noise WHERE text_hash = ?", (h,)).fetchone() is not None


def remember_noise(clean_diff: str):
    if len(clean_diff) < 25:
        return
    h = hashlib.sha256(clean_diff[:1000].lower().encode("utf-8")).hexdigest()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO learned_noise (text_hash) VALUES (?)", (h,))


def load_bewerber_profil() -> str:
    if os.path.exists("/app/profil.txt"):
        with open("/app/profil.txt", "r", encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text
    return os.environ.get("BEWERBER_PROFIL", DEFAULT_PROFIL).strip()


def clean_html_text(raw: str) -> str:
    text = re.sub(r"<br\s*/?>", "\n", raw, flags=re.IGNORECASE)
    text = re.sub(r"<[^>]+>", " ", text)
    text = html.unescape(text)
    text = text.replace("\xa0", " ")
    text = re.sub(r"[ \t]+", " ", text)
    return text.strip()


def extract_visible_text(raw_html: str) -> str:
    txt = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw_html, flags=re.IGNORECASE | re.DOTALL)
    txt = re.sub(r"</(p|div|li|tr|h[1-6]|br)>", "\n", txt, flags=re.IGNORECASE)
    return strip_cookie_lines(clean_html_text(txt))


def strip_cookie_lines(text: str) -> str:
    kept = []
    for line in text.splitlines():
        low = line.lower().strip()
        if not low:
            continue
        if any(marker in low for marker in COOKIE_NOISE_MARKERS):
            continue
        kept.append(line.strip())
    return "\n".join(kept)


def human_ago(ts_str: str) -> str:
    if not ts_str:
        return "noch nie"
    try:
        diff = int(time.time() - float(ts_str))
    except ValueError:
        return "unbekannt"
    if diff < 60:
        return f"vor {diff} Sek."
    if diff < 3600:
        return f"vor {diff // 60} Min."
    if diff < 86400:
        return f"vor {diff // 3600} Std."
    return f"vor {diff // 86400} Tagen"


def parse_docker_logs(raw: bytes) -> str:
    out = []
    i = 0
    while i + 8 <= len(raw):
        stream_type = raw[i]
        if stream_type in (0, 1, 2) and raw[i + 1 : i + 4] == b"\x00\x00\x00":
            size = int.from_bytes(raw[i + 4 : i + 8], "big")
            i += 8
            out.append(raw[i : i + size].decode("utf-8", errors="replace"))
            i += size
        else:
            return raw.decode("utf-8", errors="replace")
    return "".join(out) if out else raw.decode("utf-8", errors="replace")


def docker_client():
    transport = httpx.AsyncHTTPTransport(uds=DOCKER_SOCK)
    return httpx.AsyncClient(transport=transport, base_url="http://docker", timeout=30)


init_db()


class ListingAnalysis(BaseModel):
    ist_neues_mietangebot: bool = Field(
        description="True bei einem konkreten neuen Wohnungs-Mietangebot. False bei Löschungen, 'vermietet', Cookie-Bannern, Menüs oder allgemeinen News."
    )
    passt_zu_suche: bool = Field(
        description="True NUR, wenn Budget und gelernte Nutzerregeln passen UND es zu 100% KEINE Tauschwohnung / kein Wohnungstausch, KEIN Gewerbe, KEIN Stellplatz und KEIN Senioren-/Pflegeheim ist."
    )
    ablehnungsgrund: str = Field(description="Kurzer Grund, falls False; sonst leer.")
    objekt_titel: str = Field(description="Kurzer Titel, z. B. '2-Zi.-Altbauwohnung Maxvorstadt'")
    preis: str = Field(description="Miete, z. B. '890 € kalt' oder 'nicht angegeben'")
    groesse_zimmer: str = Field(description="Zimmer und Fläche, z. B. '1,5 Zi. / 38 m²'")
    lage: str = Field(description="Stadtteil oder Straße in München")
    deal_check: str = Field(description="Kurze Einschätzung (Schnäppchen / Fair / Knapp)")
    warnungen: list[str] = Field(default_factory=list)
    anschreiben: str = Field(description="Kurzes, individuelles Anschreiben inkl. Codewörtern & Kontaktdaten aus dem Profil.")


class SilentFailureItem(BaseModel):
    uuid: str
    url: str
    diagnosis: str
    auto_fix_applied: bool = True


class HealthAudit(BaseModel):
    detected_silent_failures: list[SilentFailureItem] = Field(default_factory=list)
    false_negative_findings: str = ""
    rule_hygiene_summary: str = ""


class WatchCreateAction(BaseModel):
    name: str
    url: str
    fetch_backend: str = Field(description="'html_requests' oder 'html_webdriver'")
    include_filters: list[str] = Field(default_factory=list)
    webdriver_delay: int = 3
    check_interval_minutes: int = 10
    confidence: float = 0.85
    reason: str = ""


class WatchChanges(BaseModel):
    fetch_backend: str = ""
    update_include_filters: bool = False
    include_filters: list[str] = Field(default_factory=list)
    webdriver_delay: int = 0
    check_interval_minutes: int = 0
    paused: bool = False


class WatchUpdateAction(BaseModel):
    uuid: str
    changes: WatchChanges
    reason: str = ""


class WatchDeleteAction(BaseModel):
    uuid: str
    url: str
    reason: str = ""


class Actions5050(BaseModel):
    create: list[WatchCreateAction] = Field(default_factory=list)
    update: list[WatchUpdateAction] = Field(default_factory=list)
    delete: list[WatchDeleteAction] = Field(default_factory=list)


class ActionsBrain(BaseModel):
    update_rules: bool = False
    consolidated_learned_rules: list[str] = Field(default_factory=list)
    update_negative_keywords: bool = False
    negative_keywords: list[str] = Field(default_factory=list)
    blacklist_urls: list[str] = Field(default_factory=list)


class MetaAgentPlan(BaseModel):
    health_audit: HealthAudit
    actions_5050: Actions5050
    actions_8000_brain: ActionsBrain
    next_discovery_queries: list[str] = Field(default_factory=list)


def is_obvious_tausch(text: str) -> bool:
    low = text.lower()
    if any(w in low for w in ["tauschwohnung.com", "immoswap", "wohnungsswap"]):
        return True
    tausch_markers = ["tauschwohnung", "wohnungstausch", "tauschangebot", "tauschbörse", "tauschpartner", "nur im tausch", "nur zum tausch"]
    if any(m in low for m in tausch_markers):
        if not any(neg in low for neg in ["keine tausch", "kein tausch", "kein wohnungstausch", "nicht zum tausch"]):
            return True
    return False


def hits_negative_keyword(text: str) -> str:
    low = text.lower()
    for kw in get_negative_keywords():
        if kw in low:
            return kw
    return ""


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


async def send_listing_alert(title: str, watch_url: str, result: ListingAnalysis, listing_id: int = 0) -> tuple[bool, str]:
    warn_block = ""
    if result.warnungen:
        joined = " | ".join(html.escape(w) for w in result.warnungen)
        warn_block = f"\n⚠️ <b>Hinweise:</b> {joined}"

    msg = (
        f"💎 <b>NEUER TREFFER – {html.escape(title or 'Münchner Anbieter')}</b>\n\n"
        f"🏠 <b>{html.escape(result.objekt_titel)}</b>\n"
        f"💶 <b>Miete:</b> {html.escape(result.preis)}\n"
        f"📐 <b>Größe:</b> {html.escape(result.groesse_zimmer)}\n"
        f"📍 <b>Lage:</b> {html.escape(result.lage)}\n"
        f"📊 <b>Check:</b> {html.escape(result.deal_check)}"
        f"{warn_block}\n\n"
        f"🔗 {html.escape(watch_url)}\n\n"
        f"✉️ <b>Anschreiben (zum Kopieren):</b>\n"
        f"<pre>{html.escape(result.anschreiben)}</pre>"
    )
    markup = None
    if listing_id > 0:
        markup = {
            "inline_keyboard": [[
                {"text": "👍 Top", "callback_data": f"fb:ok:{listing_id}"},
                {"text": "👎 Müll/Tausch", "callback_data": f"fb:trash:{listing_id}"},
                {"text": "📍 Lage falsch", "callback_data": f"fb:loc:{listing_id}"},
                {"text": "💶 Zu teuer", "callback_data": f"fb:price:{listing_id}"},
            ]]
        }
    return await send_telegram(msg, reply_markup=markup)


def is_already_seen(fingerprint: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT 1 FROM seen_listings WHERE fingerprint = ?", (fingerprint,)).fetchone() is not None


def save_seen_listing(fingerprint: str, title: str, url: str, lage: str = "", preis: str = "") -> int:
    with sqlite3.connect(DB_PATH) as conn:
        cur = conn.execute(
            "INSERT OR IGNORE INTO seen_listings (fingerprint, title, url, lage, preis) VALUES (?, ?, ?, ?, ?)",
            (fingerprint, title, url, lage, preis),
        )
        if cur.lastrowid:
            return cur.lastrowid
        row = conn.execute("SELECT id FROM seen_listings WHERE fingerprint = ?", (fingerprint,)).fetchone()
        return row[0] if row else 0


def is_url_checked(url: str) -> bool:
    if not url:
        return False
    h = hashlib.sha256(url.lower().strip().encode("utf-8")).hexdigest()
    with sqlite3.connect(DB_PATH) as conn:
        return conn.execute("SELECT 1 FROM checked_urls WHERE url_hash = ?", (h,)).fetchone() is not None


def mark_url_checked(url: str):
    if not url:
        return
    h = hashlib.sha256(url.lower().strip().encode("utf-8")).hexdigest()
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute("INSERT OR IGNORE INTO checked_urls (url_hash) VALUES (?)", (h,))


def parse_payload(data: dict) -> tuple[str, str, str]:
    title = clean_html_text(str(data.get("title", "")))
    message = str(data.get("message", "")).strip()
    watch_url = ""
    diff_text = message

    url_match = re.search(r"^URL:\s*(.+)$", message, flags=re.MULTILINE)
    if url_match:
        watch_url = clean_html_text(url_match.group(1))
    if "DIFF:\n" in message:
        diff_text = message.split("DIFF:\n", 1)[1].strip()
    elif "DIFF:" in message:
        diff_text = message.split("DIFF:", 1)[1].strip()

    diff_text = strip_cookie_lines(clean_html_text(diff_text))
    return title, watch_url, diff_text


def analyze_with_gemini(title: str, watch_url: str, diff_text: str) -> ListingAnalysis:
    api_key = get_gemini_key()
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
    profil = load_bewerber_profil()
    budget = get_budget()
    warm_tol = budget + 80
    rules = get_learned_rules()
    rules_block = "\n".join(f"- {r}" for _, r in rules) if rules else "(noch keine zusätzlichen Nutzerregeln)"

    prompt = f"""
Du bist ein knallharter, präziser Wohnungs-Filter für München (Zentrum, ca. 5 km um den Hauptbahnhof, Budget max. {budget} € Miete).
Analysiere dieses Angebot von "{title}" ({watch_url}). Es kann ein Website-Ausschnitt ODER ein strukturiertes Portal-Inserat (JSON von Fredy/ImmoScout/Kleinanzeigen/Immowelt) sein.

BEWERBER-PROFIL FÜR DAS ANSCHREIBEN:
{profil}

GELERNTE PERSÖNLICHE REGELN DES NUTZERS (zwingend beachten!):
{rules_block}

STRENGE REGELN:
1. NULL TOLERANZ BEI TAUSCHWOHNUNGEN: Prüfe jedes Wort auf Wohnungstausch! Wenn irgendwo 'Tausch', 'Tauschwohnung', 'Wohnungstausch', 'Tauschpartner', 'nur im Tausch', 'biete ... suche ...', 'gegen größere/kleinere Wohnung abgeben' oder ein Tausch-Portal erwähnt wird (außer es steht ausdrücklich 'KEINE Tauschwohnung') -> passt_zu_suche = false!
2. Wenn der Text nur gelöschte/vermietete Wohnungen, Navigation, Cookie-Banner, Uhrzeiten oder 'keine Angebote' zeigt -> ist_neues_mietangebot = false.
3. Wenn es ein konkretes Mietangebot ist -> ist_neues_mietangebot = true. Budget-Check <= {budget} € (falls Warmmiete bis ~{warm_tol} € oder kein Preis genannt wird -> true).
4. Stellplatz, Garage, Büro/Gewerbe, Seniorenwohnen oder reine Ferienwohnung -> passt_zu_suche = false.
5. Achte auf versteckte Hinweise (WBS, Indexmiete, Staffelmiete, befristet) und Codewörter für die Bewerbung.

TEXT / DATEN DES ANGEBOTS:
{diff_text[:12000]}
"""
    last_exc = None
    for model_name in MODELS_FALLBACK:
        try:
            cfg_kwargs = {
                "response_mime_type": "application/json",
                "response_schema": ListingAnalysis,
            }
            if model_name == "gemini-4-hydrogen":
                cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="LOW")

            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg_kwargs),
            )
            set_state("last_model", model_name)
            set_state("last_error", "")
            return ListingAnalysis.model_validate_json(response.text)
        except Exception as exc:
            last_exc = exc
            err_str = str(exc)
            if any(code in err_str for code in ["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "404"]):
                continue
            set_state("last_error", err_str[:220])
            raise
    set_state("last_error", str(last_exc)[:220])
    raise last_exc


async def fetch_stealth(url: str) -> tuple[int, str]:
    try:
        async with CffiSession(impersonate="chrome", timeout=15) as session:
            resp = await session.get(url, allow_redirects=True)
            return resp.status_code, resp.text or ""
    except Exception as exc:
        logger.warning("curl_cffi Fehler für %s: %s", url, exc)
        return 0, ""


def is_challenge_page(text: str) -> bool:
    low = text.lower()
    return any(m in low for m in ["just a moment", "cf-browser-verification", "attention required", "access denied", "captcha", "enable javascript and cookies", "bitte aktivieren sie javascript"])


def _css_has_text(soup: BeautifulSoup, selector: str) -> bool:
    try:
        els = soup.select(selector)
        return bool(els) and any(el.get_text(strip=True) for el in els)
    except Exception:
        return False


async def ensure_cd_tag(client: httpx.AsyncClient, tag_name: str = "auto-learned") -> str:
    cd_key = get_cd_api_key()
    try:
        r = await client.get(f"{CD_API_BASE}/tags", headers={"x-api-key": cd_key})
        if r.status_code == 200:
            for tid, tdata in (r.json() or {}).items():
                if str(tdata.get("title", "")).lower() == tag_name.lower():
                    return tid
        cr = await client.post(f"{CD_API_BASE}/tag", headers={"x-api-key": cd_key}, json={"title": tag_name})
        if cr.status_code in (200, 201):
            return (cr.json() or {}).get("uuid", "")
    except Exception:
        pass
    return ""


async def run_autonomous_agent(manual: bool = False, focus_district: str = ""):
    cd_key = get_cd_api_key()
    api_key = get_gemini_key()
    if not cd_key or not is_real_key(api_key):
        if manual:
            await send_telegram("ℹ️ Für den Meta-Lern-Agenten müssen <code>/cdkey</code> und Gemini-Key gesetzt sein.")
        return

    if manual:
        await send_telegram("🧠 <b>Meta-Optimierer gestartet...</b>\nPrüfe Silent Failures, IGNORED-Stichproben, Regel-Hygiene & neue Münchner Suchvektoren.")

    watches = {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{CD_API_BASE}/watch", headers={"x-api-key": cd_key})
            if r.status_code == 200:
                watches = r.json()
    except Exception as exc:
        if manual:
            await send_telegram(f"❌ 5050-API nicht erreichbar: {html.escape(str(exc)[:150])}")
        return

    now_ts = time.time()
    sem = asyncio.Semaphore(5)

    async def inspect_watch(uuid: str, w: dict) -> dict:
        u = str(w.get("url", ""))
        err = str(w.get("last_error") or "")
        if err:
            record_source_stat(u, str(w.get("title") or ""), err=1, last_error=err)
        last_ch = float(w.get("last_changed") or 0)
        days_no_diff = round((now_ts - last_ch) / 86400, 1) if last_ch > 0 else None
        text_len = None
        preview = ""
        if not w.get("paused") and not ("localhost:" in u or "127.0.0.1:" in u):
            async with sem:
                try:
                    async with httpx.AsyncClient(timeout=8) as c:
                        hr = await c.get(f"{CD_API_BASE}/watch/{uuid}/history/latest", headers={"x-api-key": cd_key})
                        if hr.status_code == 200:
                            txt = hr.text or ""
                            text_len = len(txt.strip())
                            preview = re.sub(r"\s+", " ", txt).strip()[:220]
                except Exception:
                    pass
        return {
            "uuid": uuid,
            "title": w.get("title") or "",
            "url": u,
            "fetch_backend": w.get("fetch_backend") or "system",
            "include_filters": w.get("include_filters") or [],
            "paused": bool(w.get("paused")),
            "days_since_last_diff": days_no_diff,
            "last_text_length": text_len,
            "snapshot_preview": preview,
            "last_error": err[:160],
        }

    active_watches = await asyncio.gather(*(inspect_watch(u, w) for u, w in watches.items()))

    with sqlite3.connect(DB_PATH) as conn:
        stats_rows = conn.execute(
            "SELECT source_key, title, triggers_total, true_positives, ignored_noise, errors_count, last_error FROM source_stats ORDER BY triggers_total DESC LIMIT 40"
        ).fetchall()
        fb_rows = conn.execute(
            "SELECT title, url, lage, preis, feedback FROM seen_listings WHERE feedback != '' ORDER BY id DESC LIMIT 15"
        ).fetchall()
        ign_rows = conn.execute(
            "SELECT source_url, title, reason, snippet FROM ignored_samples ORDER BY id DESC LIMIT 10"
        ).fetchall()
        bl_rows = conn.execute("SELECT pattern, reason FROM blacklist_urls").fetchall()
        rules_rows = conn.execute("SELECT rule FROM learned_rules ORDER BY id DESC").fetchall()
        neg_kw_rows = conn.execute("SELECT keyword FROM negative_keywords").fetchall()

    try:
        stored_queries = json.loads(get_state("next_discovery_queries", "[]") or "[]")
    except Exception:
        stored_queries = []
    search_queries = stored_queries[:4] if stored_queries else DEFAULT_DISCOVERY_QUERIES
    if focus_district:
        search_queries = [
            f"Hausverwaltung {focus_district} München Mietwohnungen Angebote",
            f"Wohnungsbaugenossenschaft {focus_district} München freie Wohnungen",
        ] + search_queries[:2]

    runtime_context = {
        "budget_eur": get_budget(),
        "performance_stats": [
            {"source": r[0], "title": r[1], "triggers": r[2], "true_positives": r[3], "ignored": r[4], "errors": r[5], "last_error": r[6]}
            for r in stats_rows
        ],
        "ignored_samples": [
            {"source_url": r[0], "title": r[1], "reason": r[2], "snippet": r[3]}
            for r in ign_rows
        ],
        "recent_feedback": [
            {"title": r[0], "url": r[1], "lage": r[2], "preis": r[3], "feedback": r[4]}
            for r in fb_rows
        ],
        "current_learned_rules": [r[0] for r in rules_rows],
        "negative_keywords": [r[0] for r in neg_kw_rows],
        "active_watches": active_watches,
        "blacklist": [{"pattern": r[0], "reason": r[1]} for r in bl_rows],
        "search_history_queries_used": search_queries,
    }

    known_doms = sorted({urlparse(w["url"]).netloc.lower() for w in active_watches if w["url"]})
    search_prompt = (
        f"Nutze Google Search für diese konkreten Suchanfragen (Münchner Immobilien unter {get_budget()} €, Radius 5 km um Hbf):\n"
        + "\n".join(f"- {q}" for q in search_queries)
        + f"\n\nBereits überwachte Domains (NICHT nochmal nennen): {', '.join(known_doms[:50])}\n"
        + f"Blacklist: {', '.join(b['pattern'] for b in runtime_context['blacklist'][:30])}\n"
        + "Finde 5-8 neue, verifizierte Angebots-Unterseiten (auch wenn dort 'aktuell keine freien Wohnungen' steht). Keine Großportale."
    )

    def _phase1_search() -> tuple[str, list[str]]:
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        for m in ("gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.5-flash-lite"):
            try:
                resp = client.models.generate_content(
                    model=m,
                    contents=search_prompt,
                    config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
                )
                urls = set(re.findall(r"https?://[^\s\"'<>)\]]+", resp.text or ""))
                meta = resp.candidates[0].grounding_metadata if resp.candidates else None
                if meta and meta.grounding_chunks:
                    for ch in meta.grounding_chunks:
                        if getattr(ch, "web", None) and getattr(ch.web, "uri", None):
                            urls.add(ch.web.uri)
                return resp.text or "", list(urls)
            except Exception as exc:
                if any(c in str(exc) for c in ["429", "RESOURCE_EXHAUSTED", "503", "400", "404"]):
                    continue
                break
        return "", []

    search_text, search_urls = await asyncio.to_thread(_phase1_search)

    meta_prompt = f"""
### ROLLE: AUTONOMER SYSTEM-ARCHITEKT & SELF-EVOLVING AGENT

Du bist das übergeordnete Optimierungs- und Lern-Gehirn für das gesamte Wohnungs-Monitoring-System (`brain` auf :8000, `changedetection.io` auf :5050, `fredy` auf :9998).
Dein Ziel ist es, die Trefferquote in München (Radius 5 km um Hbf, Budget bis {get_budget()} €) kontinuierlich zu maximieren, stille Systemfehler selbstständig aufzuspüren und das System ohne menschliches Eingreifen immer präziser zu machen.

### AUFGABE 1: PROAKTIVE FEHLERSUCHE & STILLE AUSFÄLLE AUFSPÜREN
1. **Stille Tode erkennen (Silent Failures):**
   - Wenn eine aktive Watch-URL in `:5050` seit > 21 Tagen 0 Diffs hat (`days_since_last_diff > 21`) UND `last_text_length < 150` oder ein kaputter `include_filters`-Selektor vorliegt: Erzeuge ein `UPDATE` (`update_include_filters: true`, `include_filters: []`, `fetch_backend: "html_webdriver"`, `webdriver_delay: 4`).
   - Hinweis: Steht im `snapshot_preview` sinnvoll "Aktuell keine freien Wohnungen" mit normaler Textlänge, ist das KEIN kaputter Selektor, sondern eine ruhige Genossenschaft!
   - Defekte `localhost:`-Links -> `DELETE`.
2. **Bot-Blocker & Captcha-Fallen entlarven:**
   - Enthält `snapshot_preview` oder `last_error` Wörter wie "Just a moment", "Cloudflare", "Access Denied", "Captcha": Erhöhe `webdriver_delay` auf 4-5 oder wechsle auf `"html_webdriver"`.
3. **False-Negative-Audit:**
   - Prüfe `ignored_samples` kritisch: Wurde versehentlich eine gute Wohnung unter {get_budget()} € aussortiert? Falls ja, formuliere eine Korrekturregel in `consolidated_learned_rules`.
4. **Regel-Hygiene & Anti-Bloat:**
   - Konsolidiere `current_learned_rules` auf max. 10 kompakte, widerspruchsfreie Regeln (`update_rules: true` nur wenn du Regeln veränderst/bereinigst; lasse bestehende gute Regeln erhalten!).

### AUFGABE 2: DYNAMISCHE DISCOVERY & INTERVALL-OPTIMIERUNG
- Erstelle in `actions_5050.create` NUR echte, verifizierte neue URLs aus den Websuche-Ergebnissen (nie erfinden, keine Dubletten zu `active_watches` oder `blacklist`).
- Setze `check_interval_minutes` intelligent: 5-8 Min. bei dynamischen Verwaltern, 15-30 Min. bei trägen Stiftungen/Genossenschaften (nie unter 5).
- Liefere in `next_discovery_queries` 3-5 unverbrauchte Google-Suchanfragen für Münchner Nischen (Stiftungen, Pfründestiftungen, VDIV, Nachbarviertel) für den nächsten Lauf.

LAUFZEITDATEN (JSON):
{json.dumps(runtime_context, ensure_ascii=False)[:15000]}

FRISCHE WEBSUCHE-ERGEBNISSE:
{search_text[:3500]}
GEFUNDENE KANDIDATEN-URLS:
{json.dumps(search_urls[:25], ensure_ascii=False)}
"""

    def _phase2_plan() -> MetaAgentPlan:
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        for m in MODELS_FALLBACK:
            try:
                cfg_kwargs = {
                    "response_mime_type": "application/json",
                    "response_schema": MetaAgentPlan,
                }
                if m == "gemini-4-hydrogen":
                    cfg_kwargs["thinking_config"] = types.ThinkingConfig(thinking_level="LOW")
                resp = client.models.generate_content(
                    model=m,
                    contents=meta_prompt,
                    config=types.GenerateContentConfig(**cfg_kwargs),
                )
                return MetaAgentPlan.model_validate_json(resp.text)
            except Exception as exc:
                if any(c in str(exc) for c in ["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "404"]):
                    continue
                raise
        raise RuntimeError("Alle KI-Modelle im Meta-Lauf blockiert.")

    try:
        plan = await asyncio.to_thread(_phase2_plan)
    except Exception as exc:
        if manual:
            await send_telegram(f"❌ Meta-Agent fehlgeschlagen: <i>{html.escape(str(exc)[:180])}</i>")
        return

    set_state("last_agent_plan_json", plan.model_dump_json(indent=2))
    set_state("last_agent_run", str(time.time()))

    if plan.next_discovery_queries:
        set_state("next_discovery_queries", json.dumps(plan.next_discovery_queries[:5], ensure_ascii=False))

    existing_urls_norm = {w["url"].rstrip("/").lower() for w in active_watches}
    existing_domains = {urlparse(w["url"]).netloc.lower().replace("www.", "") for w in active_watches if w["url"]}
    executed_log = []

    async with httpx.AsyncClient(timeout=20) as client:
        for d in plan.actions_5050.delete:
            if d.uuid in watches:
                dr = await client.delete(f"{CD_API_BASE}/watch/{d.uuid}", headers={"x-api-key": cd_key})
                if dr.status_code in (200, 204):
                    add_blacklist(d.url, d.reason)
                    executed_log.append(f"🗑 Gelöscht: <code>{html.escape(d.url[:55])}</code>")

        for u in plan.actions_5050.update:
            if u.uuid not in watches:
                continue
            patch = {}
            if u.changes.fetch_backend in ("html_requests", "html_webdriver"):
                patch["fetch_backend"] = u.changes.fetch_backend
                if u.changes.fetch_backend == "html_webdriver":
                    dom = urlparse(str(watches[u.uuid].get("url", ""))).netloc.lower()
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute("INSERT OR REPLACE INTO domain_rules (domain, mode, strikes) VALUES (?, 'chrome', 0)", (dom,))
            if u.changes.webdriver_delay > 0:
                patch["webdriver_delay"] = min(10, u.changes.webdriver_delay)
            if u.changes.check_interval_minutes > 0:
                patch["time_between_check"] = {"minutes": max(5, min(1440, u.changes.check_interval_minutes))}
                patch["time_between_check_use_default"] = False
            if u.changes.paused:
                patch["paused"] = True
            if u.changes.update_include_filters:
                if not u.changes.include_filters:
                    patch["include_filters"] = []
                else:
                    st, raw_h = await fetch_stealth(str(watches[u.uuid].get("url", "")))
                    if st == 200 and raw_h:
                        soup = BeautifulSoup(raw_h, "html.parser")
                        valid_f = [sel for sel in u.changes.include_filters if _css_has_text(soup, sel)]
                        patch["include_filters"] = valid_f
            if patch:
                ur = await client.put(f"{CD_API_BASE}/watch/{u.uuid}", headers={"x-api-key": cd_key}, json=patch)
                if ur.status_code in (200, 204):
                    executed_log.append(f"🔧 Optimiert: <b>{html.escape(str(watches[u.uuid].get('title') or u.uuid)[:42])}</b> ({html.escape(u.reason[:65])})")

        tag_uuid = await ensure_cd_tag(client, "auto-learned")
        for c in plan.actions_5050.create:
            if c.confidence < 0.70 or not c.url.startswith("http"):
                continue
            c_norm = c.url.rstrip("/").lower()
            dom = urlparse(c_norm).netloc.lower().replace("www.", "")
            if not dom or c_norm in existing_urls_norm or dom in existing_domains or is_blacklisted(c_norm):
                continue
            if any(ex in dom for ex in EXCLUDED_DISCOVERY_DOMAINS):
                continue

            status, raw_h = await fetch_stealth(c.url)
            if status != 200 or is_challenge_page(raw_h):
                continue
            vis = extract_visible_text(raw_h)
            if len(vis) < 120 or not IMMOBILIEN_SIGNAL_RE.search(vis):
                continue

            payload = {
                "url": c.url,
                "title": f"[Auto] {c.name[:70]}",
                "fetch_backend": c.fetch_backend if c.fetch_backend in ("html_requests", "html_webdriver") else "html_webdriver",
                "time_between_check": {"minutes": max(5, min(60, c.check_interval_minutes or 10))},
                "time_between_check_use_default": False,
            }
            if c.webdriver_delay > 0 and payload["fetch_backend"] == "html_webdriver":
                payload["webdriver_delay"] = min(8, c.webdriver_delay)
            if tag_uuid:
                payload["tags"] = [tag_uuid]
            if c.include_filters and payload["fetch_backend"] == "html_requests":
                soup = BeautifulSoup(raw_h, "html.parser")
                valid_f = [sel for sel in c.include_filters if _css_has_text(soup, sel)]
                if valid_f:
                    payload["include_filters"] = valid_f

            cr = await client.post(f"{CD_API_BASE}/watch", headers={"x-api-key": cd_key}, json=payload)
            if cr.status_code in (200, 201):
                existing_urls_norm.add(c_norm)
                existing_domains.add(dom)
                executed_log.append(f"➕ Neu überwacht: <b>{html.escape(c.name)}</b>\n   {html.escape(c.url)}")

    if plan.actions_8000_brain.update_rules and plan.actions_8000_brain.consolidated_learned_rules:
        set_consolidated_rules(plan.actions_8000_brain.consolidated_learned_rules)
        executed_log.append(f"🧹 Regeln auf <b>{len(plan.actions_8000_brain.consolidated_learned_rules[:10])}</b> kompakte Regeln konsolidiert")

    if plan.actions_8000_brain.update_negative_keywords and plan.actions_8000_brain.negative_keywords:
        set_consolidated_negative_keywords(plan.actions_8000_brain.negative_keywords)

    for bu in plan.actions_8000_brain.blacklist_urls:
        add_blacklist(bu, "Meta-Agent-Blacklist")

    sf_count = len(plan.health_audit.detected_silent_failures)
    act_lines = "\n".join(executed_log[:12]) or "• Keine Änderungen nötig – System läuft optimal."
    next_q = "\n".join(f"• {html.escape(q[:70])}" for q in plan.next_discovery_queries[:3])

    report = (
        f"🤖 <b>Meta-Optimierung abgeschlossen</b>\n\n"
        f"🩺 <b>Audit:</b> {sf_count} stille Ausfälle erkannt\n"
        f"🔍 <b>False-Negatives:</b> <i>{html.escape((plan.health_audit.false_negative_findings or 'Keine')[:180])}</i>\n"
        f"🧹 <b>Regel-Hygiene:</b> <i>{html.escape((plan.health_audit.rule_hygiene_summary or 'Sauber')[:150])}</i>\n\n"
        f"<b>Ausgeführte Aktionen:</b>\n{act_lines}"
    )
    if next_q:
        report += f"\n\n<b>Nächste Suchvektoren:</b>\n{next_q}"
    report += "\n\n<i>Komplettes JSON mit /agent json</i>"
    await send_telegram(report)


async def run_auto_heal(manual: bool = False):
    cd_key = get_cd_api_key()
    if not cd_key:
        if manual:
            await send_telegram("ℹ️ Erst <code>/cdkey DEIN_5050_KEY</code> setzen.")
        return

    actions = []
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            resp = await client.get(f"{CD_API_BASE}/watch", headers={"x-api-key": cd_key})
            if resp.status_code != 200:
                return
            watches = resp.json()

            for uuid, w in watches.items():
                url = str(w.get("url", "")).strip()
                err = str(w.get("last_error") or "")
                paused = bool(w.get("paused"))
                backend = str(w.get("fetch_backend") or "")
                domain = urlparse(url).netloc.lower()

                if "localhost:" in url or "127.0.0.1:" in url:
                    dr = await client.delete(f"{CD_API_BASE}/watch/{uuid}", headers={"x-api-key": cd_key})
                    if dr.status_code in (200, 204):
                        add_blacklist(url, "Defekter Selbst-Link")
                        actions.append(f"🗑 Kaputten Selbst-Link gelöscht: <code>{html.escape(url)}</code>")
                    continue

                if paused or not err:
                    continue

                record_source_stat(url, str(w.get("title") or ""), err=1, last_error=err)
                err_low = err.lower()
                if backend == "html_requests" and any(k in err_low for k in ["403", "401", "forbidden", "access denied", "empty"]):
                    await client.put(f"{CD_API_BASE}/watch/{uuid}", headers={"x-api-key": cd_key}, json={"fetch_backend": "html_webdriver"})
                    with sqlite3.connect(DB_PATH) as conn:
                        conn.execute("INSERT OR REPLACE INTO domain_rules (domain, mode, strikes) VALUES (?, 'chrome', 0)", (domain,))
                    actions.append(f"🌐 <b>{html.escape(domain)}</b> blockte Blitz-Modus → auf Chrome umgestellt!")
                    continue

                if any(k in err_low for k in ["403", "access denied", "forbidden", "cloudflare"]):
                    status, raw_html = await fetch_stealth(url)
                    vis = extract_visible_text(raw_html) if status == 200 else ""
                    if status == 200 and len(vis) > 200 and not is_challenge_page(raw_html):
                        await client.put(f"{CD_API_BASE}/watch/{uuid}", headers={"x-api-key": cd_key}, json={"paused": True})
                        with sqlite3.connect(DB_PATH) as conn:
                            conn.execute(
                                "INSERT OR REPLACE INTO stealth_watches (url, title, last_text, fail_count) VALUES (?, ?, ?, 0)",
                                (url, str(w.get("title") or domain), vis),
                            )
                        actions.append(f"🕵️‍♂️ <b>{html.escape(domain)}</b> per Tarnumhang gerettet! Überwache sie alle 5 Min. direkt.")
                    else:
                        with sqlite3.connect(DB_PATH) as conn:
                            row = conn.execute("SELECT strikes FROM domain_rules WHERE domain = ?", (domain,)).fetchone()
                            strikes = (row[0] if row else 0) + 1
                            conn.execute("INSERT OR REPLACE INTO domain_rules (domain, mode, strikes) VALUES (?, 'blocked', ?)", (domain, strikes))
                        if strikes >= 3:
                            await client.put(f"{CD_API_BASE}/watch/{uuid}", headers={"x-api-key": cd_key}, json={"paused": True})
                            add_blacklist(url, "Dauerhafter 403-Bot-Block")
                            actions.append(f"🛑 <b>{html.escape(domain)}</b> blockt jeden Bot (3x 403) → pausiert.")

        if actions:
            await send_telegram("🩹 <b>Selbstheilung hat aufgeräumt:</b>\n\n" + "\n".join(actions))
        elif manual:
            await send_telegram("✅ <b>Selbstheilung-Check:</b> Keine kaputten Links oder 403-Blocker offen.")
    except Exception as exc:
        logger.exception("Auto-Heal Fehler:")
        if manual:
            await send_telegram(f"❌ Selbstheilung-Fehler: {html.escape(str(exc)[:150])}")


def build_status_text() -> str:
    paused = get_state("paused", "0") == "1"
    mute_err = get_state("mute_errors", "0") == "1"
    last_cd = human_ago(get_state("last_cd_webhook", ""))
    last_fr = human_ago(get_state("last_fredy_webhook", ""))
    last_ag = human_ago(get_state("last_agent_run", ""))
    last_model = get_state("last_model", "noch keins")
    last_err = get_state("last_error", "") or "Keiner ✅"
    filtered = get_state("filtered_count", "0")
    budget = get_budget()
    rules_cnt = len(get_learned_rules())
    neg_cnt = len(get_negative_keywords())
    with sqlite3.connect(DB_PATH) as conn:
        hits = conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]
        stealth_cnt = conn.execute("SELECT COUNT(*) FROM stealth_watches").fetchone()[0]
        ign_cnt = conn.execute("SELECT COUNT(*) FROM ignored_samples").fetchone()[0]

    status_line = "⏸ <b>PAUSIERT</b> (mit /weiter aktivieren)" if paused else "🟢 <b>AKTIV</b> (mit /pause stoppen)"
    err_mode = "🔇 Fehler-Push stumm" if mute_err else "🔔 Fehler-Push aktiv"

    return (
        f"📊 <b>Immobot-Status (Self-Evolving 3.1)</b>\n\n"
        f"• Zustand: {status_line}\n"
        f"• Fehlermeldungen: {err_mode}\n"
        f"• Max. Budget: <b>{budget} €</b>\n"
        f"• 5050-Signal: <b>{last_cd}</b> | Fredy: <b>{last_fr}</b>\n"
        f"• Letzter Meta-Agent: <b>{last_ag}</b>\n"
        f"• Tarnumhang: <b>{stealth_cnt}</b> | Audit-Samples: <b>{ign_cnt}</b>\n"
        f"• Aktive Regeln: <b>{rules_cnt}/10</b> | Negativ-Wörter: <b>{neg_cnt}</b>\n"
        f"• Letztes KI-Modell: <code>{html.escape(last_model)}</code>\n"
        f"• Aussortiert: <b>{filtered}</b> | Treffer: <b>{hits}</b>\n"
        f"• Letzter Fehler: <i>{html.escape(last_err)}</i>\n\n"
        f"Tippe /hilfe für alle Befehle."
    )


def help_text() -> str:
    return (
        "🛠 <b>Telegram-Fernsteuerung &amp; Meta-Agent</b>\n\n"
        "<b>Selbstheilung &amp; Meta-Optimierung:</b>\n"
        "• /scout [check|Stadtteil] – 5-km-Geo-Radar + Deep-Link-Jäger (fügt neue Verwalter ein)\n• /agent [Stadtteil] – Startet Silent-Failure-Audit, Regel-Hygiene &amp; Discovery\n"
        "• /agent json – Zeigt das letzte JSON-Protokoll des Meta-Agenten\n"
        "• /heilen – Löscht kaputte URLs &amp; rettet 403-Seiten per Tarnumhang\n"
        "• /stats – Treffer-/Rausch-Statistik deiner Quellen\n"
        "• /lerne REGEL | /regeln – Eigene KI-Regeln ansehen\n\n"
        "<b>Überwachung &amp; Steuerung:</b>\n"
        "• /status | /pause | /weiter | /ruhe | /test\n"
        "• /budget 1100 | /setkey AIza...\n\n"
        "<b>Docker, Logs &amp; 5050:</b>\n"
        "• /docker | /logs fredy (oder 5050, chrome, brain)\n"
        "• /restart chrome (oder fredy, 5050, all)\n"
        "• /turbo | /fehler | /browser 1 | /pausieren 1 | /recheck | /add URL"
    )


async def cmd_stats():
    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT title, source_key, triggers_total, true_positives, ignored_noise, errors_count FROM source_stats ORDER BY triggers_total DESC LIMIT 12"
        ).fetchall()
    if not rows:
        await send_telegram("📊 Noch keine Quellen-Statistiken gesammelt.")
        return
    lines = ["📊 <b>Top-Quellen-Statistik:</b>\n"]
    for title, src, trig, tp, ign, err in rows:
        name = title or urlparse(src).netloc or src
        lines.append(f"• <b>{html.escape(str(name)[:38])}</b>: {trig}x | ✅ {tp} | 🗑 {ign} | ⚠️ {err}")
    await send_telegram("\n".join(lines))


async def cmd_docker_status():
    if not os.path.exists(DOCKER_SOCK):
        await send_telegram("❌ Docker-Socket fehlt.")
        return
    try:
        async with docker_client() as client:
            resp = await client.get("/containers/json", params={"all": "1"})
            lines = ["🐳 <b>Docker-Container auf dem Fujitsu:</b>\n"]
            for c in resp.json():
                name = (c.get("Names") or ["?"])[0].lstrip("/")
                icon = "🟢" if c.get("State") == "running" else "🔴"
                lines.append(f"{icon} <b>{html.escape(name)}</b>: {html.escape(c.get('Status', ''))}")
            await send_telegram("\n".join(lines))
    except Exception as exc:
        await send_telegram(f"❌ Docker-Fehler: {html.escape(str(exc)[:150])}")


async def cmd_docker_logs(target: str):
    if not os.path.exists(DOCKER_SOCK):
        await send_telegram("❌ Docker-Socket fehlt.")
        return
    cname = CONTAINER_ALIASES.get(target.lower(), target)
    try:
        async with docker_client() as client:
            resp = await client.get(f"/containers/{cname}/logs", params={"stdout": "1", "stderr": "1", "tail": "30"})
            if resp.status_code != 200:
                await send_telegram(f"❌ Container <code>{html.escape(cname)}</code> nicht gefunden.")
                return
            text = parse_docker_logs(resp.content).strip() or "(keine aktuellen Logs)"
            await send_telegram(f"📜 <b>Logs von {html.escape(cname)}:</b>\n<pre>{html.escape(text[-3200:])}</pre>")
    except Exception as exc:
        await send_telegram(f"❌ Log-Fehler: {html.escape(str(exc)[:150])}")


async def cmd_docker_restart(target: str):
    if not os.path.exists(DOCKER_SOCK):
        await send_telegram("❌ Docker-Socket fehlt.")
        return
    key = target.lower().strip()
    targets = ["changedetection-chrome", "changedetection", "fredy"] if key == "all" else [CONTAINER_ALIASES.get(key, target)]
    if "immobot-brain" in targets:
        await send_telegram("🔄 <b>immobot-brain</b> startet jetzt neu...")
    try:
        async with docker_client() as client:
            ok_list = []
            for cname in targets:
                resp = await client.post(f"/containers/{cname}/restart", params={"t": "5"})
                if resp.status_code in (200, 204):
                    ok_list.append(cname)
            if ok_list and "immobot-brain" not in ok_list:
                await send_telegram(f"✅ Neu gestartet: <b>{html.escape(', '.join(ok_list))}</b>")
    except Exception as exc:
        await send_telegram(f"❌ Neustart-Fehler: {html.escape(str(exc)[:150])}")


async def cmd_cd_errors():
    cd_key = get_cd_api_key()
    if not cd_key:
        await send_telegram("ℹ️ Erst <code>/cdkey DEIN_5050_KEY</code> senden.")
        return
    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(f"{CD_API_BASE}/watch", headers={"x-api-key": cd_key})
            if resp.status_code != 200:
                await send_telegram(f"❌ 5050-API Fehler: {resp.status_code}")
                return
            watches = resp.json()
        err_list = [(uuid, w.get("title") or w.get("url") or uuid, str(w.get("last_error")))
                    for uuid, w in watches.items() if w.get("last_error") and not w.get("paused")]
        if not err_list:
            await send_telegram(f"✅ Alle <b>{len(watches)}</b> Seiten in 5050 laufen ohne Fehler!")
            return
        set_state("last_error_uuids", json.dumps([e[0] for e in err_list]))
        lines = [f"⚠️ <b>{len(err_list)} fehlerhafte Seiten in 5050:</b>\n"]
        for idx, (_, name, err) in enumerate(err_list[:15], 1):
            lines.append(f"<b>{idx}.</b> {html.escape(str(name)[:60])}\n   <i>{html.escape(err[:100])}</i>")
        lines.append("\n👉 Mit <code>/heilen</code> automatisch reparieren/retten, oder <code>/browser 1</code> / <code>/pausieren 1</code>.")
        await send_telegram("\n".join(lines))
    except Exception as exc:
        await send_telegram(f"❌ Fehler: {html.escape(str(exc)[:150])}")


async def cmd_cd_turbo():
    cd_key = get_cd_api_key()
    if not cd_key:
        await send_telegram("ℹ️ Erst <code>/cdkey DEIN_5050_KEY</code> setzen.")
        return
    try:
        with sqlite3.connect(DB_PATH) as conn:
            chrome_domains = {r[0] for r in conn.execute("SELECT domain FROM domain_rules WHERE mode = 'chrome'").fetchall()}
        async with httpx.AsyncClient(timeout=30) as client:
            resp = await client.get(f"{CD_API_BASE}/watch", headers={"x-api-key": cd_key})
            watches = resp.json()
            fast_cnt, js_cnt = 0, 0
            for uuid, w in watches.items():
                url = str(w.get("url", "")).lower()
                dom = urlparse(url).netloc.lower()
                needs_js = any(d in url for d in JS_REQUIRED_DOMAINS) or dom in chrome_domains
                backend = "html_webdriver" if needs_js else "html_requests"
                r = await client.put(f"{CD_API_BASE}/watch/{uuid}", headers={"x-api-key": cd_key}, json={"fetch_backend": backend})
                if r.status_code in (200, 204):
                    if backend == "html_requests":
                        fast_cnt += 1
                    else:
                        js_cnt += 1
            await client.get(f"{CD_API_BASE}/watch", params={"recheck_all": "1"}, headers={"x-api-key": cd_key})
        await send_telegram(f"⚡ <b>5050-Turbo aktiviert!</b>\n• <b>{fast_cnt}</b> Blitz-Modus (0,3s)\n• <b>{js_cnt}</b> Chrome (inkl. gelernter Domains)")
    except Exception as exc:
        await send_telegram(f"❌ Turbo-Fehler: {html.escape(str(exc)[:150])}")


async def cmd_cd_browser(arg: str):
    cd_key = get_cd_api_key()
    try:
        idx = int(arg.strip()) - 1
        uuid = json.loads(get_state("last_error_uuids", "[]"))[idx]
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(f"{CD_API_BASE}/watch/{uuid}", headers={"x-api-key": cd_key}, json={"fetch_backend": "html_webdriver", "paused": False})
            if resp.status_code in (200, 204):
                await send_telegram(f"🌐 Seite <b>#{idx + 1}</b> läuft jetzt auf Chrome!")
    except Exception:
        await send_telegram("❌ Erst <code>/fehler</code> aufrufen, dann z. B. <code>/browser 1</code>.")


async def cmd_cd_pause(arg: str):
    cd_key = get_cd_api_key()
    try:
        idx = int(arg.strip()) - 1
        uuid = json.loads(get_state("last_error_uuids", "[]"))[idx]
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.put(f"{CD_API_BASE}/watch/{uuid}", headers={"x-api-key": cd_key}, json={"paused": True})
            if resp.status_code in (200, 204):
                await send_telegram(f"⏸ Seite <b>#{idx + 1}</b> pausiert.")
    except Exception:
        await send_telegram("❌ Erst <code>/fehler</code> aufrufen, dann z. B. <code>/pausieren 1</code>.")


async def cmd_cd_recheck():
    cd_key = get_cd_api_key()
    if not cd_key:
        await send_telegram("ℹ️ Erst <code>/cdkey DEIN_KEY</code> setzen.")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        await client.get(f"{CD_API_BASE}/watch", params={"recheck_all": "1"}, headers={"x-api-key": cd_key})
    await send_telegram("🔄 Alle aktiven Seiten in 5050 werden neu geprüft!")


async def cmd_cd_add(url: str):
    cd_key = get_cd_api_key()
    if not cd_key or not url.startswith("http"):
        await send_telegram("❌ Gültige URL und <code>/cdkey</code> nötig.")
        return
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(f"{CD_API_BASE}/watch", headers={"x-api-key": cd_key}, json={"url": url})
        if resp.status_code in (200, 201):
            await send_telegram(f"✅ Seite zu 5050 hinzugefügt:\n{html.escape(url)}")




async def run_scout_command(auto_add: bool = True, district: str = "", manual: bool = True):
    cd_key = get_cd_api_key()
    api_key = get_gemini_key()
    if not cd_key or not is_real_key(api_key):
        if manual:
            await send_telegram("ℹ️ Für /scout müssen <code>/cdkey</code> und Gemini-Key gesetzt sein.")
        return

    if manual:
        await send_telegram(f"🔭 <b>Münchner 5-km-Scout gestartet...</b>\nDurchsuche Geo-Radar + Websuche nach neuen Hausverwaltungen &amp; prüfe Deep-Links + iFrames live.")

    watches = {}
    try:
        async with httpx.AsyncClient(timeout=20) as client:
            r = await client.get(f"{CD_API_BASE}/watch", headers={"x-api-key": cd_key})
            if r.status_code == 200:
                watches = r.json()
    except Exception as exc:
        if manual:
            await send_telegram(f"❌ 5050 nicht erreichbar: {html.escape(str(exc)[:150])}")
        return

    if len(watches) >= 65:
        if manual:
            await send_telegram("ℹ️ Sicherheits-Limit von 65 Seiten in 5050 erreicht, damit dein Fujitsu schnell bleibt.")
        return

    known_domains = {urlparse(str(w.get("url", ""))).netloc.lower().replace("www.", "") for w in watches.values() if w.get("url")}
    with sqlite3.connect(DB_PATH) as conn:
        for (u,) in conn.execute("SELECT url FROM stealth_watches").fetchall():
            known_domains.add(urlparse(u).netloc.lower().replace("www.", ""))

    found = await scout.run_scout_pipeline(
        api_key=api_key,
        known_domains=known_domains,
        blacklist_check_fn=is_blacklisted,
        db_path=DB_PATH,
        budget=get_budget(),
        district=district,
        max_probe=12,
    )

    added = 0
    for item in found:
        c, p, e = item["candidate"], item["page"], item["eval"]
        if not item["accepted"]:
            add_blacklist(c["domain"], f"Scout aussortiert: {e.ausschlussgrund}")
            continue

        if auto_add and added < 5 and (len(watches) + added) < 65:
            async with httpx.AsyncClient(timeout=20) as client:
                tag_uuid = await ensure_cd_tag(client, "auto-scout")
                payload = {
                    "url": p["url"],
                    "title": f"[Scout] {e.firmenname[:65]}",
                    "fetch_backend": p["fetch_backend"],
                    "time_between_check": {"minutes": max(5, min(30, e.empfohlenes_intervall_min))},
                    "time_between_check_use_default": False,
                }
                if p["fetch_backend"] == "html_webdriver":
                    payload["webdriver_delay"] = 4
                if tag_uuid:
                    payload["tags"] = [tag_uuid]
                if item["valid_filters"]:
                    payload["include_filters"] = item["valid_filters"]
                cr = await client.post(f"{CD_API_BASE}/watch", headers={"x-api-key": cd_key}, json=payload)
                if cr.status_code in (200, 201):
                    w_uuid = (cr.json() or {}).get("uuid", "")
                    added += 1
                    markup = {"inline_keyboard": [[{"text": "🗑 Doch raus & Blacklist", "callback_data": f"sdel:{w_uuid}"}]]} if w_uuid else None
                    leer_badge = " (aktuell 0 Whg – Top 24/7-Ziel!)" if e.hat_leerstand_hinweis else ""
                    await send_telegram(
                        f"➕ <b>Neuer Scout-Treffer hinzugefügt!</b>{leer_badge}\n\n"
                        f"🏢 <b>{html.escape(e.firmenname)}</b>\n"
                        f"📍 {html.escape(e.bezirk_hinweis)} ({c['dist_km']} km zum Hbf)\n"
                        f"⚙️ Technik: <code>{p['fetch_backend']}</code> | Alle {payload['time_between_check']['minutes']} Min.\n"
                        f"💡 <i>{html.escape(e.kurzbegruendung[:160])}</i>\n"
                        f"🔗 {html.escape(p['url'])}",
                        reply_markup=markup,
                    )
        else:
            await send_telegram(
                f"🔎 <b>Scout-Vorschlag:</b> <b>{html.escape(e.firmenname)}</b> ({c['dist_km']} km zum Hbf)\n"
                f"🔗 {html.escape(p['url'])}\n<i>{html.escape(e.kurzbegruendung[:140])}</i>\n"
                f"Aufnehmen mit: <code>/add {html.escape(p['url'])}</code>"
            )

    if manual and added == 0:
        await send_telegram("ℹ️ Scout-Durchlauf fertig – aktuell keine weiteren neuen Münchner Verwaltungen in diesem Batch gefunden.")


async def handle_callback_query(cb: dict):
    cb_id = cb.get("id", "")
    from_id = str((cb.get("from") or {}).get("id", ""))
    chat_id = str(((cb.get("message") or {}).get("chat") or {}).get("id", ""))
    if from_id != TELEGRAM_CHAT_ID and chat_id != TELEGRAM_CHAT_ID:
        return

    data = cb.get("data") or ""
    answer_txt = "Gespeichert!"

    if data.startswith("fb:"):
        _, kind, lid_str = data.split(":", 2)
        with sqlite3.connect(DB_PATH) as conn:
            row = conn.execute("SELECT title, url, lage, preis FROM seen_listings WHERE id = ?", (int(lid_str),)).fetchone()
            conn.execute("UPDATE seen_listings SET feedback = ? WHERE id = ?", (kind, int(lid_str)))
        t, u, lage, preis = row if row else ("Wohnung", "", "", "")
        if kind == "ok":
            record_source_stat(u, t, tp=1)
            answer_txt = "👍 Super! Als Top-Treffer gelernt."
        elif kind == "trash":
            record_source_stat(u, t, ignore=1)
            add_learned_rule(f"Kein Interesse an Angeboten wie '{t}' (war Tausch/Müll).")
            answer_txt = "🧠 Gelernt! Künftig strenger rausgefiltert."
        elif kind == "loc":
            record_source_stat(u, t, ignore=1)
            if lage:
                add_learned_rule(f"Lage '{lage}' ablehnen (dem Nutzer zu weit/unpassend).")
            answer_txt = f"📍 Gelernt! Lage '{lage or t}' wird vermieden."
        elif kind == "price":
            record_source_stat(u, t, ignore=1)
            add_learned_rule(f"Angebote im Preisniveau wie '{preis}' bei '{t}' eher ablehnen.")
            answer_txt = "💶 Gelernt! Strengerer Preis-Check."
        await send_telegram(f"🧠 <b>Feedback gelernt:</b> {html.escape(answer_txt)}\n<i>Regeln ansehen mit /regeln</i>")

    elif data.startswith("sdel:"):
        w_uuid = data.split(":", 1)[1]
        cd_key = get_cd_api_key()
        if cd_key and w_uuid:
            async with httpx.AsyncClient(timeout=15) as client:
                wr = await client.get(f"{CD_API_BASE}/watch/{w_uuid}", headers={"x-api-key": cd_key})
                if wr.status_code == 200:
                    w_url = (wr.json() or {}).get("url", "")
                    if w_url:
                        add_blacklist(w_url, "Manuell vom Nutzer aus Scout entfernt")
                dr = await client.delete(f"{CD_API_BASE}/watch/{w_uuid}", headers={"x-api-key": cd_key})
            answer_txt = "🗑 Gelöscht & auf Blacklist gesetzt!" if dr.status_code in (200, 204) else "Schon gelöscht."
            await send_telegram(f"🗑 {html.escape(answer_txt)}")

    async with httpx.AsyncClient(timeout=10) as client:
        await client.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/answerCallbackQuery",
            json={"callback_query_id": cb_id, "text": answer_txt[:180]},
        )


async def handle_telegram_command(text: str):
    parts = text.strip().split(maxsplit=1)
    cmd = parts[0].lower()
    arg = parts[1].strip() if len(parts) > 1 else ""

    if cmd == "/pause":
        set_state("paused", "1")
        await send_telegram("⏸ <b>Bot pausiert.</b> Weiter mit /weiter.")
    elif cmd in ("/weiter", "/resume"):
        set_state("paused", "0")
        await send_telegram("🟢 <b>Bot wieder aktiv!</b>")
    elif cmd == "/ruhe":
        cur = get_state("mute_errors", "0") == "1"
        set_state("mute_errors", "0" if cur else "1")
        await send_telegram("🔇 <b>KI-Fehlerwarnungen stumm.</b>" if not cur else "🔔 <b>KI-Fehlerwarnungen wieder AN.</b>")
    elif cmd == "/test":
        res = await run_test_wohnung()
        if not res.get("ok"):
            await send_telegram(f"❌ Test fehlgeschlagen: {html.escape(str(res.get('error')))}")
    elif cmd == "/budget":
        if arg.isdigit():
            set_state("max_budget", arg)
            await send_telegram(f"💶 Maximal-Budget auf <b>{arg} €</b> gesetzt!")
        else:
            await send_telegram(f"Aktuell: <b>{get_budget()} €</b>. Ändern: <code>/budget 1100</code>")
    elif cmd == "/setkey":
        if len(arg) > 20:
            set_state("gemini_api_key_override", arg)
            await send_telegram("🔑 Neuer Gemini-Key gespeichert! Test mit /test")
    elif cmd == "/cdkey":
        if len(arg) > 8:
            set_state("cd_api_key", arg)
            await cmd_cd_errors()
    elif cmd == "/lerne":
        if arg:
            add_learned_rule(arg)
            await send_telegram(f"🧠 <b>Regel dauerhaft gelernt:</b>\n„{html.escape(arg)}“")
        else:
            await send_telegram("Beispiel: <code>/lerne Keine Wohnungen im Erdgeschoss</code>")
    elif cmd == "/regeln":
        if arg.startswith("del "):
            rid = arg.split(" ", 1)[1].strip()
            if rid.isdigit():
                with sqlite3.connect(DB_PATH) as conn:
                    conn.execute("DELETE FROM learned_rules WHERE id = ?", (int(rid),))
                await send_telegram(f"🗑 Regel #{rid} gelöscht.")
                return
        rules = get_learned_rules()
        neg = get_negative_keywords()
        lines = ["🧠 <b>Aktive gelernte KI-Regeln (max. 10 nach Konsolidierung):</b>"]
        lines.extend([f"<b>#{i}</b> {html.escape(r)}" for i, r in rules] or ["(noch keine Regeln)"])
        if neg:
            lines.append(f"\n🚫 <b>Negativ-Keywords:</b> <code>{html.escape(', '.join(neg[:25]))}</code>")
        lines.append("\nLöschen mit <code>/regeln del NR</code>")
        await send_telegram("\n".join(lines))
    elif cmd == "/stats":
        await cmd_stats()
    elif cmd == "/heilen":
        await run_auto_heal(manual=True)
    elif cmd == "/scout":
        auto_mode = arg.lower() != "check"
        dist_arg = "" if arg.lower() in ("check", "auto") else arg
        asyncio.create_task(run_scout_command(auto_add=auto_mode, district=dist_arg, manual=True))
    elif cmd in ("/agent", "/entdecken", "/lernen"):
        if arg.lower() == "json":
            raw_j = get_state("last_agent_plan_json", "")
            await send_telegram(f"<pre>{html.escape((raw_j or 'Noch kein Agenten-Lauf.')[:3600])}</pre>")
        else:
            asyncio.create_task(run_autonomous_agent(manual=True, focus_district=arg))
    elif cmd == "/turbo":
        await cmd_cd_turbo()
    elif cmd == "/browser":
        await cmd_cd_browser(arg)
    elif cmd == "/docker":
        await cmd_docker_status()
    elif cmd == "/logs":
        await cmd_docker_logs(arg or "brain")
    elif cmd == "/restart":
        await cmd_docker_restart(arg or "chrome")
    elif cmd == "/fehler":
        await cmd_cd_errors()
    elif cmd == "/pausieren":
        await cmd_cd_pause(arg)
    elif cmd == "/recheck":
        await cmd_cd_recheck()
    elif cmd == "/add":
        await cmd_cd_add(arg)
    elif cmd in ("/hilfe", "/help"):
        await send_telegram(help_text())
    elif cmd in ("/status", "/start"):
        await send_telegram(build_status_text())


async def telegram_poll_loop():
    offset = 0
    while True:
        try:
            if not is_real_key(TELEGRAM_TOKEN) or not is_real_key(TELEGRAM_CHAT_ID):
                await asyncio.sleep(10)
                continue
            url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/getUpdates"
            async with httpx.AsyncClient(timeout=35) as client:
                resp = await client.get(url, params={"offset": offset, "timeout": 25})
                if resp.status_code != 200:
                    await asyncio.sleep(5)
                    continue
                for upd in resp.json().get("result", []):
                    offset = upd["update_id"] + 1
                    if "callback_query" in upd:
                        await handle_callback_query(upd["callback_query"])
                        continue
                    msg = upd.get("message") or {}
                    chat_id = str((msg.get("chat") or {}).get("id", ""))
                    text = msg.get("text") or ""
                    if chat_id == TELEGRAM_CHAT_ID and text.startswith("/"):
                        await handle_telegram_command(text)
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Telegram-Poll Pause: %s", exc)
            await asyncio.sleep(5)


async def stealth_poll_loop():
    await asyncio.sleep(30)
    while True:
        try:
            if get_state("paused", "0") != "1":
                with sqlite3.connect(DB_PATH) as conn:
                    rows = conn.execute("SELECT url, title, last_text, fail_count FROM stealth_watches").fetchall()
                for url, title, last_text, fails in rows:
                    status, raw_html = await fetch_stealth(url)
                    if status == 200 and not is_challenge_page(raw_html):
                        new_vis = extract_visible_text(raw_html)
                        old_lines = {l.strip() for l in (last_text or "").splitlines() if l.strip()}
                        added = "\n".join(l.strip() for l in new_vis.splitlines() if l.strip() and l.strip() not in old_lines)
                        with sqlite3.connect(DB_PATH) as conn:
                            conn.execute("UPDATE stealth_watches SET last_text = ?, fail_count = 0, updated_at = CURRENT_TIMESTAMP WHERE url = ?", (new_vis, url))
                        if last_text and len(added.strip()) >= 25:
                            await process_cd_webhook({"title": f"{title} (Tarnumhang)", "message": f"URL: {url}\nDIFF:\n{added}"})
                    else:
                        with sqlite3.connect(DB_PATH) as conn:
                            conn.execute("UPDATE stealth_watches SET fail_count = ? WHERE url = ?", (fails + 1, url))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Stealth-Loop Hinweis: %s", exc)
        await asyncio.sleep(300)


async def watchdog_and_heal_loop():
    last_restart = {}
    heal_tick = 0
    await asyncio.sleep(60)
    while True:
        try:
            if os.path.exists(DOCKER_SOCK):
                async with docker_client() as client:
                    resp = await client.get("/containers/json", params={"all": "1"})
                    if resp.status_code == 200:
                        for c in resp.json():
                            cname = (c.get("Names") or [""])[0].lstrip("/")
                            if cname in ("changedetection", "changedetection-chrome", "fredy") and c.get("State") in ("exited", "dead"):
                                now = time.time()
                                if now - last_restart.get(cname, 0) > 1800:
                                    last_restart[cname] = now
                                    r = await client.post(f"/containers/{cname}/restart", params={"t": "5"})
                                    if r.status_code in (200, 204):
                                        await send_telegram(f"🔧 <b>Selbstheilung:</b> Container <code>{html.escape(cname)}</code> neu gestartet!")
            heal_tick += 1
            if heal_tick % 3 == 0:
                await run_auto_heal(manual=False)
            last_ag = float(get_state("last_agent_run", "0") or "0")
            if time.time() - last_ag > 86400 and get_cd_api_key() and is_real_key(get_gemini_key()):
                set_state("last_agent_run", str(time.time()))
                asyncio.create_task(run_autonomous_agent(manual=False))
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("Watchdog Hinweis: %s", exc)
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    t1 = asyncio.create_task(telegram_poll_loop())
    t2 = asyncio.create_task(watchdog_and_heal_loop())
    t3 = asyncio.create_task(stealth_poll_loop())
    try:
        yield
    finally:
        t1.cancel()
        t2.cancel()
        t3.cancel()


app = FastAPI(lifespan=lifespan)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    tg_ok = is_real_key(TELEGRAM_TOKEN) and is_real_key(TELEGRAM_CHAT_ID)
    gem_ok = is_real_key(get_gemini_key())
    prof_ok = os.path.exists("/app/profil.txt") and os.path.getsize("/app/profil.txt") > 0
    paused = get_state("paused", "0") == "1"
    last_cd = human_ago(get_state("last_cd_webhook", ""))
    last_fr = human_ago(get_state("last_fredy_webhook", ""))
    last_ag = human_ago(get_state("last_agent_run", ""))

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute("SELECT title, url, created_at FROM seen_listings ORDER BY created_at DESC LIMIT 20").fetchall()
        total_count = conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]
        rules_cnt = conn.execute("SELECT COUNT(*) FROM learned_rules").fetchone()[0]

    rows_html = "".join(
        f"<tr><td>{html.escape(ts or '')}</td><td><b>{html.escape(t or 'Wohnung')}</b></td><td><a href='{html.escape(u or '#')}' target='_blank'>Öffnen ↗</a></td></tr>"
        for t, u, ts in rows
    ) or "<tr><td colspan='3' style='color:#94a3b8'>Noch keine echten Treffer durchgelaufen.</td></tr>"

    badge = lambda ok: "<span style='color:#22c55e;font-weight:bold'>✅ Aktiv</span>" if ok else "<span style='color:#ef4444;font-weight:bold'>❌ Fehlt</span>"
    pause_badge = "<span style='color:#f59e0b;font-weight:bold'>⏸ Pausiert</span>" if paused else "<span style='color:#22c55e;font-weight:bold'>🟢 Läuft</span>"

    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Immobot München – Self-Evolving KI-Zentrale</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#0f172a; color:#f8fafc; max-width:900px; margin:40px auto; padding:0 20px; }}
  .card {{ background:#1e293b; border-radius:12px; padding:20px; margin-bottom:20px; border:1px solid #334155; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(160px,1fr)); gap:12px; }}
  .stat {{ background:#0f172a; padding:14px; border-radius:8px; }}
  a.btn {{ display:inline-block; background:#3b82f6; color:white; text-decoration:none; padding:10px 16px; border-radius:8px; font-weight:600; margin:4px 8px 4px 0; }}
  a.btn.sec {{ background:#334155; }}
  a {{ color:#60a5fa; }}
  table {{ width:100%; border-collapse:collapse; margin-top:10px; }}
  th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid #334155; font-size:14px; }}
</style></head>
<body>
  <h1>🧠 Immobot München – Self-Evolving KI-Zentrale</h1>
  <div class="card grid">
    <div class="stat"><div>Bot-Status</div>{pause_badge}</div>
    <div class="stat"><div>Telegram-Bot</div>{badge(tg_ok)}</div>
    <div class="stat"><div>Gemini + Meta-Agent</div>{badge(gem_ok)}</div>
    <div class="stat"><div>Bewerber-Profil</div>{badge(prof_ok)}</div>
    <div class="stat"><div>Gelernte Regeln</div><b>{rules_cnt}/10</b></div>
    <div class="stat"><div>Letzter Meta-Agent</div><b>{html.escape(last_ag)}</b></div>
    <div class="stat"><div>5050 / Fredy</div><b>{html.escape(last_cd)} / {html.escape(last_fr)}</b></div>
    <div class="stat"><div>Gefundene Treffer</div><b>{total_count}</b></div>
  </div>
  <div class="card">
    <h3>Schnellzugriff</h3>
    <a class="btn" href="/test-wohnung" target="_blank">🧪 Test-Wohnung an Telegram senden</a>
    <a class="btn sec" id="link5050" href="#">🏠 Hausverwaltungen (5050)</a>
    <a class="btn sec" id="link9998" href="#">🔎 Portale / Fredy (9998)</a>
  </div>
  <div class="card">
    <h3>Letzte erkannte Treffer (Datenbank)</h3>
    <table><tr><th>Zeit</th><th>Wohnung</th><th>Link</th></tr>{rows_html}</table>
  </div>
  <script>
    document.getElementById('link5050').href = location.protocol + '//' + location.hostname + ':5050';
    document.getElementById('link9998').href = location.protocol + '//' + location.hostname + ':9998';
  </script>
</body></html>"""


@app.get("/health")
def health():
    return {
        "status": "ok",
        "paused": get_state("paused", "0") == "1",
        "docker_sock": os.path.exists(DOCKER_SOCK),
        "telegram_configured": is_real_key(TELEGRAM_TOKEN),
        "chat_id_configured": is_real_key(TELEGRAM_CHAT_ID),
        "gemini_configured": is_real_key(get_gemini_key()),
        "profil_geladen": os.path.exists("/app/profil.txt") and os.path.getsize("/app/profil.txt") > 0,
    }


async def run_test_wohnung():
    if not is_real_key(get_gemini_key()):
        return {"ok": False, "error": "GEMINI_API_KEY ist noch nicht gesetzt."}
    fake_title = "Test-Hausverwaltung München (Simulation)"
    fake_url = "https://beispiel-hausverwaltung-muenchen.de/mietangebote"
    fake_diff = (
        "NEU AB SOFORT: Helle 1,5-Zimmer-Altbauwohnung in München-Maxvorstadt, "
        "39 qm, 3. OG mit kleinem Südbalkon, Einbauküche vorhanden, 860 EUR Kaltmiete + 130 EUR Nebenkosten. "
        "Ruhiger Innenhof, 8 Min zu Fuß zur U-Bahn Universität. "
        "WICHTIG: Um automatische Anfragen auszusortieren, schreiben Sie bitte das Codewort 'Isarwelle' "
        "in Ihre Nachricht an Frau Berger."
    )
    try:
        result = await asyncio.to_thread(analyze_with_gemini, fake_title, fake_url, fake_diff)
        fp_raw = f"test|{fake_url}|{result.objekt_titel}"
        lid = save_seen_listing(hashlib.sha256(fp_raw.encode()).hexdigest(), result.objekt_titel, fake_url, result.lage, result.preis)
        ok, info = await send_listing_alert(fake_title, fake_url, result, listing_id=lid)
        return {"ok": ok, "telegram": info, "analyse": result.model_dump()}
    except Exception as exc:
        logger.exception("Test-Wohnung Fehler:")
        return {"ok": False, "error": str(exc)}


@app.get("/test-wohnung")
async def test_wohnung():
    return await run_test_wohnung()


async def process_cd_webhook(data: dict):
    try:
        title, watch_url, diff_text = parse_payload(data)
        lower_all = f"{title} {watch_url} {diff_text}".lower()
        if "#notifications" in lower_all or "example text" in lower_all or ("changedetection.io" in lower_all and ("test" in lower_all or len(diff_text) < 40)):
            key_status = "gesetzt ✅" if is_real_key(get_gemini_key()) else "FEHLT NOCH ❌"
            await send_telegram(f"🧠 <b>Immobot-Gehirn verbunden!</b>\nGemini Key: <b>{key_status}</b>")
            return

        if get_state("paused", "0") == "1":
            return

        record_source_stat(watch_url, title, trigger=1)
        clean_diff = re.sub(r"\s+", " ", diff_text).strip()
        if len(clean_diff) < 25:
            record_source_stat(watch_url, ignore=1)
            inc_counter("filtered_count")
            return

        if not IMMOBILIEN_SIGNAL_RE.search(clean_diff):
            save_ignored_sample(watch_url, title, "Kein Immobilien-Signalwort", clean_diff)
            record_source_stat(watch_url, ignore=1)
            inc_counter("filtered_count")
            return

        if is_learned_noise(clean_diff):
            record_source_stat(watch_url, ignore=1)
            inc_counter("filtered_count")
            return

        neg_hit = hits_negative_keyword(f"{title} {clean_diff}")
        if neg_hit:
            save_ignored_sample(watch_url, title, f"Negativ-Keyword: {neg_hit}", clean_diff)
            record_source_stat(watch_url, ignore=1)
            inc_counter("filtered_count")
            return

        if is_obvious_tausch(f"{title} {watch_url} {clean_diff}"):
            save_ignored_sample(watch_url, title, "Tauschwohnungs-Marker", clean_diff)
            record_source_stat(watch_url, ignore=1)
            inc_counter("filtered_count")
            return

        if not is_real_key(get_gemini_key()):
            await send_telegram(f"⚠️ <b>Kein Gemini-Key!</b> Roh-Update von <b>{html.escape(title)}</b>:\n{html.escape(watch_url)}")
            return

        try:
            result = await asyncio.to_thread(analyze_with_gemini, title, watch_url, diff_text)
        except Exception as exc:
            if get_state("mute_errors", "0") != "1":
                await send_telegram(
                    f"⚠️ <b>KI-Pause:</b> <b>{html.escape(title)}</b>\n{html.escape(watch_url)}\n\n"
                    f"{html.escape(clean_diff[:450])}\n\n<i>Fehler: {html.escape(str(exc)[:140])}</i>"
                )
            return

        if not result.ist_neues_mietangebot:
            remember_noise(clean_diff)
            save_ignored_sample(watch_url, title, result.ablehnungsgrund or "Kein Mietangebot", clean_diff)
            record_source_stat(watch_url, ignore=1)
            inc_counter("filtered_count")
            return

        if not result.passt_zu_suche:
            save_ignored_sample(watch_url, title, result.ablehnungsgrund or "Passt nicht zu Suche", clean_diff)
            record_source_stat(watch_url, ignore=1)
            inc_counter("filtered_count")
            return

        fp_raw = f"{watch_url.lower()}|{result.objekt_titel.lower()}|{result.preis.lower()}|{result.groesse_zimmer.lower()}"
        fingerprint = hashlib.sha256(fp_raw.encode("utf-8")).hexdigest()
        if is_already_seen(fingerprint):
            return

        record_source_stat(watch_url, tp=1)
        lid = save_seen_listing(fingerprint, result.objekt_titel, watch_url, result.lage, result.preis)
        await send_listing_alert(title, watch_url, result, listing_id=lid)
    except Exception:
        logger.exception("Webhook Hintergrundfehler:")


@app.post("/webhook")
async def webhook(request: Request):
    set_state("last_cd_webhook", str(time.time()))
    data = await request.json()
    asyncio.create_task(process_cd_webhook(data))
    return {"status": "accepted"}


def extract_fredy_items(payload) -> list[dict]:
    if isinstance(payload, list):
        return [x for x in payload if isinstance(x, dict)]
    if isinstance(payload, dict):
        for key in ("listings", "items", "data", "results"):
            val = payload.get(key)
            if isinstance(val, list):
                return [x for x in val if isinstance(x, dict)]
        return [payload]
    return []



async def fetch_immoscout_contact_details(expose_url: str) -> tuple[Optional[str], Optional[str], Optional[str]]:
    """
    Zieht aus einem ImmoScout-Exposé die echte E-Mail, Telefonnummer und Firmennamen
    des Anbieters aus den gesetzlichen Angaben / Impressum.
    """
    try:
        status, final_u, raw_html = await scout.fetch_page(expose_url)
        if status != 200 or len(raw_html) < 500:
            return None, None, None

        soup = BeautifulSoup(raw_html, "html.parser")
        email_found = None
        phone_found = None
        company_found = None

        # 1. Nach rechtlichen Hinweisen / Impressumsblock suchen
        legal_block = soup.find(class_=re.compile(r"(legal|imprint|anbieter|realtor|contact)", re.I))
        search_text = legal_block.get_text(" ", strip=True) if legal_block else raw_html

        # E-Mail mit Regex extrahieren (keine noreply / immoscout-internen Mails)
        emails = re.findall(r"[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+", search_text)
        for em in emails:
            em_low = em.lower()
            if not any(ign in em_low for ign in ("immobilienscout24", "is24", "noreply", "no-reply", "datenschutz", "spamschutz")):
                email_found = em.strip()
                break

        # Telefonnummer suchen
        phones = re.findall(r"(?:(?:\+49|0049|0)\s*(?:\(?\d+\)?\s*[\d\s/-]{5,}))", search_text)
        if phones:
            phone_found = phones[0].strip()

        # Firmenname suchen
        comp_tag = soup.find(class_=re.compile(r"(company-name|realtor-name|firmname)", re.I))
        if comp_tag:
            company_found = comp_tag.get_text(strip=True)

        return email_found, phone_found, company_found
    except Exception as exc:
        logger.warning("ImmoScout Impressum-Scrape Hinweis: %s", exc)
        return None, None, None


async def process_fredy_items(items: list[dict]):
    for item in items:
        try:
            item_json = json.dumps(item, ensure_ascii=False, indent=2)
            title = str(item.get("title") or item.get("name") or "Portal-Angebot").strip()
            provider = str(item.get("provider") or item.get("source") or "Fredy").strip()
            link = str(item.get("link") or item.get("url") or "").strip()

            low = item_json.lower()
            if (not link and "test" in low) or "test notification" in low or "fredy test" in low or "example.com" in low:
                await send_telegram("🧠 <b>Fredy ➡️ Brain Webhook verbunden!</b>")
                return

            if get_state("paused", "0") == "1":
                return

            if link and is_url_checked(link):
                continue

            record_source_stat(f"fredy:{provider}", provider, trigger=1)
            neg_hit = hits_negative_keyword(f"{title} {item_json}")
            if neg_hit or is_obvious_tausch(f"{title} {link} {item_json}"):
                save_ignored_sample(link or provider, title, f"Fredy Pre-Filter ({neg_hit or 'Tausch'})", item_json)
                record_source_stat(f"fredy:{provider}", ignore=1)
                inc_counter("filtered_count")
                mark_url_checked(link)
                continue

            if is_real_key(get_gemini_key()):
                # Falls ImmoScout-Link: Versuche echte Impressums-E-Mail zu holen!
                immo_email = None
                immo_phone = None
                if "immobilienscout24.de" in link.lower():
                    immo_email, immo_phone, _ = await fetch_immoscout_contact_details(link)

                try:
                    result = await asyncio.to_thread(analyze_with_gemini, f"{provider}: {title}", link, item_json)
                    if immo_email and not result.contact_email:
                        result.contact_email = immo_email
                    if immo_phone and not result.contact_phone:
                        result.contact_phone = immo_phone
                except Exception:
                    continue

                mark_url_checked(link)
                if not result.ist_neues_mietangebot or not result.passt_zu_suche:
                    save_ignored_sample(link or provider, title, result.ablehnungsgrund or "Fredy KI-Filter", item_json)
                    record_source_stat(f"fredy:{provider}", ignore=1)
                    inc_counter("filtered_count")
                    continue

                fp_raw = f"{link.lower()}|{result.objekt_titel.lower()}|{result.preis.lower()}"
                fp = hashlib.sha256(fp_raw.encode("utf-8")).hexdigest()
                if is_already_seen(fp):
                    continue

                record_source_stat(f"fredy:{provider}", tp=1)
                lid = save_seen_listing(fp, result.objekt_titel, link, result.lage, result.preis)
                await send_listing_alert(f"{provider} – {title}", link, result, listing_id=lid)
        except Exception:
            logger.exception("Fredy-Item Hintergrundfehler:")


@app.post("/fredy-webhook")
async def fredy_webhook(request: Request):
    set_state("last_fredy_webhook", str(time.time()))
    payload = await request.json()
    items = extract_fredy_items(payload)
    if not items:
        return {"status": "empty"}
    asyncio.create_task(process_fredy_items(items))
    return {"status": "accepted", "count": len(items)}
