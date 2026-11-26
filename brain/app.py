import hashlib
import html
import logging
import os
import re
import sqlite3
from fastapi import FastAPI, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
import httpx
from google import genai
from google.genai import types

logging.basicConfig(level=logging.INFO)
logger = logging.getLogger("immobot-brain")

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "").strip()
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID", "").strip()
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "").strip()
DEFAULT_PROFIL = "Berufstätig in unbefristeter Festanstellung, Nichtraucher, keine Haustiere, ruhiger Mieter, alle Unterlagen (Schufa, Gehaltsnachweise) liegen komplett vor."

DB_PATH = "/app/data/immobot.db"
os.makedirs("/app/data", exist_ok=True)


def is_real_key(val: str) -> bool:
    return bool(val) and not val.startswith("HIER_")


def load_bewerber_profil() -> str:
    if os.path.exists("/app/profil.txt"):
        with open("/app/profil.txt", "r", encoding="utf-8") as f:
            text = f.read().strip()
            if text:
                return text
    return os.environ.get("BEWERBER_PROFIL", DEFAULT_PROFIL).strip()


def init_db():
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            """
            CREATE TABLE IF NOT EXISTS seen_listings (
                fingerprint TEXT PRIMARY KEY,
                title TEXT,
                url TEXT,
                created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
            )
            """
        )


init_db()
app = FastAPI()


class ListingAnalysis(BaseModel):
    ist_neues_mietangebot: bool = Field(
        description="True nur, wenn im Text ein konkretes Wohnungs-Mietangebot neu hinzugefügt wurde. False bei Löschungen, 'vermietet', Cookie-Bannern, Menüs oder allgemeinen News."
    )
    passt_zu_suche: bool = Field(
        description="True, wenn Budget (<= 1000 EUR Miete, bzw. knapp drüber bei Warmmiete, oder kein Preis genannt) passt und es KEINE Tauschwohnung, KEIN Gewerbe, KEIN Stellplatz und KEIN Senioren-/Pflegeheim ist."
    )
    ablehnungsgrund: str = Field(
        description="Kurzer Grund, falls ist_neues_mietangebot oder passt_zu_suche False ist; sonst leer."
    )
    objekt_titel: str = Field(description="Kurzer Titel, z. B. '2-Zi.-Altbauwohnung Maxvorstadt'")
    preis: str = Field(description="Miete, z. B. '890 € kalt' oder 'nicht angegeben'")
    groesse_zimmer: str = Field(description="Zimmer und Fläche, z. B. '1,5 Zi. / 38 m²'")
    lage: str = Field(description="Stadtteil oder Straße in München")
    deal_check: str = Field(description="Kurze Einschätzung (Schnäppchen / Fair / Knapp)")
    warnungen: list[str] = Field(
        default_factory=list,
        description="Wichtige Hinweise wie 'WBS nötig', 'Tauschwohnung', 'Indexmiete', 'Befristet', oder erkannte Codewörter für die Bewerbung."
    )
    anschreiben: str = Field(
        description="Kurzes, sympathisches, individuelles Anschreiben passend zum Inserat und dem Bewerberprofil, inkl. evtl. geforderter Codewörter und Kontaktdaten aus dem Profil."
    )


async def send_telegram(text_html: str) -> tuple[bool, str]:
    if not is_real_key(TELEGRAM_TOKEN) or not is_real_key(TELEGRAM_CHAT_ID):
        return False, "TELEGRAM_TOKEN oder TELEGRAM_CHAT_ID fehlt in .env"
    if len(text_html) > 4000:
        text_html = text_html[:3990] + "\n..."
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    async with httpx.AsyncClient(timeout=15) as client:
        resp = await client.post(
            url,
            json={
                "chat_id": TELEGRAM_CHAT_ID,
                "text": text_html,
                "parse_mode": "HTML",
                "disable_web_page_preview": False,
            },
        )
        if resp.status_code != 200:
            logger.error("Telegram Fehler: %s %s", resp.status_code, resp.text)
            return False, f"Telegram {resp.status_code}: {resp.text}"
        return True, "ok"


async def send_listing_alert(title: str, watch_url: str, result: ListingAnalysis) -> tuple[bool, str]:
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
    return await send_telegram(msg)


def is_already_seen(fingerprint: str) -> bool:
    with sqlite3.connect(DB_PATH) as conn:
        row = conn.execute(
            "SELECT 1 FROM seen_listings WHERE fingerprint = ?", (fingerprint,)
        ).fetchone()
        return row is not None


def mark_as_seen(fingerprint: str, title: str, url: str):
    with sqlite3.connect(DB_PATH) as conn:
        conn.execute(
            "INSERT OR IGNORE INTO seen_listings (fingerprint, title, url) VALUES (?, ?, ?)",
            (fingerprint, title, url),
        )


def parse_payload(data: dict) -> tuple[str, str, str]:
    title = str(data.get("title", "")).strip()
    message = str(data.get("message", "")).strip()
    watch_url = ""
    diff_text = message

    url_match = re.search(r"^URL:\s*(.+)$", message, flags=re.MULTILINE)
    if url_match:
        watch_url = url_match.group(1).strip()
    if "DIFF:\n" in message:
        diff_text = message.split("DIFF:\n", 1)[1].strip()
    elif "DIFF:" in message:
        diff_text = message.split("DIFF:", 1)[1].strip()

    return title, watch_url, diff_text


def analyze_with_gemini(title: str, watch_url: str, diff_text: str) -> ListingAnalysis:
    client = genai.Client(
        api_key=GEMINI_API_KEY,
        http_options=types.HttpOptions(api_version="v1beta"),
    )
    profil = load_bewerber_profil()
    prompt = f"""
Du bist ein knallharter, präziser Wohnungs-Filter für München (Zentrum, ca. 5 km um den Hauptbahnhof, Budget max. 1.000 € Miete).
Analysiere diese neu hinzugekommene Textänderung von der Website "{title}" ({watch_url}).

BEWERBER-PROFIL FÜR DAS ANSCHREIBEN:
{profil}

REGELN:
- Wenn der Text nur gelöschte/vermietete Wohnungen, Navigation, Cookie-Banner, Uhrzeiten oder "keine Angebote" zeigt -> ist_neues_mietangebot = false.
- Wenn es ein neues Mietangebot ist: Budget-Check <= 1.000 € (falls Warmmiete bis ~1.080 € oder Preis im Text nicht genannt wird -> trotzdem true, damit nichts verpasst wird!).
- Tauschwohnung, Stellplatz, Garage, Büro/Gewerbe, Seniorenwohnen oder reine Ferienwohnung -> passt_zu_suche = false.
- Achte extrem auf versteckte Hinweise (WBS, Indexmiete, Staffelmiete, befristet) und auf Sätze wie "Schreiben Sie Codewort XY in die Nachricht".

NEUER TEXT VON DER WEBSITE:
{diff_text[:12000]}
"""
    response = client.models.generate_content(
        model="gemini-4-hydrogen",
        contents=prompt,
        config=types.GenerateContentConfig(
            response_mime_type="application/json",
            response_schema=ListingAnalysis,
            thinking_config=types.ThinkingConfig(thinking_level="LOW"),
        ),
    )
    return ListingAnalysis.model_validate_json(response.text)


@app.get("/", response_class=HTMLResponse)
def dashboard():
    tg_ok = is_real_key(TELEGRAM_TOKEN) and is_real_key(TELEGRAM_CHAT_ID)
    gem_ok = is_real_key(GEMINI_API_KEY)
    prof_ok = os.path.exists("/app/profil.txt") and os.path.getsize("/app/profil.txt") > 0

    with sqlite3.connect(DB_PATH) as conn:
        rows = conn.execute(
            "SELECT title, url, created_at FROM seen_listings ORDER BY created_at DESC LIMIT 20"
        ).fetchall()
        total_count = conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]

    rows_html = ""
    for t, u, ts in rows:
        safe_u = html.escape(u or "#")
        rows_html += (
            f"<tr><td>{html.escape(ts or '')}</td>"
            f"<td><b>{html.escape(t or 'Wohnung')}</b></td>"
            f"<td><a href='{safe_u}' target='_blank'>Öffnen ↗</a></td></tr>"
        )
    if not rows_html:
        rows_html = "<tr><td colspan='3' style='color:#94a3b8'>Noch keine echten Treffer durchgelaufen – der Monitor wartet auf die nächste neue Wohnung.</td></tr>"

    badge = lambda ok: "<span style='color:#22c55e;font-weight:bold'>✅ Aktiv</span>" if ok else "<span style='color:#ef4444;font-weight:bold'>❌ Fehlt</span>"

    return f"""<!doctype html>
<html lang="de"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1">
<title>Immobot München – KI-Zentrale</title>
<style>
  body {{ font-family: -apple-system, BlinkMacSystemFont, sans-serif; background:#0f172a; color:#f8fafc; max-width:900px; margin:40px auto; padding:0 20px; }}
  .card {{ background:#1e293b; border-radius:12px; padding:20px; margin-bottom:20px; border:1px solid #334155; }}
  .grid {{ display:grid; grid-template-columns:repeat(auto-fit,minmax(180px,1fr)); gap:12px; }}
  .stat {{ background:#0f172a; padding:14px; border-radius:8px; }}
  a.btn {{ display:inline-block; background:#3b82f6; color:white; text-decoration:none; padding:10px 16px; border-radius:8px; font-weight:600; margin:4px 8px 4px 0; }}
  a.btn.sec {{ background:#334155; }}
  a {{ color:#60a5fa; }}
  table {{ width:100%; border-collapse:collapse; margin-top:10px; }}
  th, td {{ text-align:left; padding:10px 8px; border-bottom:1px solid #334155; font-size:14px; }}
</style></head>
<body>
  <h1>🧠 Immobot München – KI-Zentrale</h1>
  <div class="card grid">
    <div class="stat"><div>Telegram-Bot</div>{badge(tg_ok)}</div>
    <div class="stat"><div>Gemini 4 Hydrogen</div>{badge(gem_ok)}</div>
    <div class="stat"><div>Bewerber-Profil</div>{badge(prof_ok)}</div>
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
        "telegram_configured": is_real_key(TELEGRAM_TOKEN),
        "chat_id_configured": is_real_key(TELEGRAM_CHAT_ID),
        "gemini_configured": is_real_key(GEMINI_API_KEY),
        "profil_geladen": os.path.exists("/app/profil.txt") and os.path.getsize("/app/profil.txt") > 0,
    }


@app.get("/test-wohnung")
async def test_wohnung():
    if not is_real_key(GEMINI_API_KEY):
        return {"ok": False, "error": "GEMINI_API_KEY in .env ist noch nicht gesetzt."}
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
        result = analyze_with_gemini(fake_title, fake_url, fake_diff)
        ok, info = await send_listing_alert(fake_title, fake_url, result)
        return {"ok": ok, "telegram": info, "analyse": result.model_dump()}
    except Exception as exc:
        logger.exception("Test-Wohnung Fehler:")
        return {"ok": False, "error": str(exc)}


@app.post("/webhook")
async def webhook(request: Request):
    data = await request.json()
    title, watch_url, diff_text = parse_payload(data)
    logger.info("Webhook erhalten für: %s (%s)", title, watch_url)

    lower_all = f"{title} {watch_url} {diff_text}".lower()
    if "#notifications" in lower_all or "example text" in lower_all or ("changedetection.io" in lower_all and ("test" in lower_all or len(diff_text) < 40)):
        key_status = "gesetzt ✅" if is_real_key(GEMINI_API_KEY) else "FEHLT NOCH ❌"
        await send_telegram(
            f"🧠 <b>Immobot-Gehirn verbunden!</b>\n"
            f"Verbindung: changedetection ➡️ Brain ➡️ Telegram klappt.\n"
            f"Gemini 4 Hydrogen Key: <b>{key_status}</b>"
        )
        return {"status": "test_sent"}

    clean_diff = re.sub(r"\s+", " ", diff_text).strip()
    if len(clean_diff) < 25:
        logger.info("Ignoriert (zu kurz / vermutlich nur Löschung).")
        return {"status": "ignored_short"}

    if not is_real_key(GEMINI_API_KEY):
        await send_telegram(
            f"⚠️ <b>Kein Gemini-Key in .env!</b> Roh-Update von <b>{html.escape(title)}</b>:\n"
            f"{html.escape(watch_url)}\n\n{html.escape(clean_diff[:600])}"
        )
        return {"status": "no_gemini_key"}

    try:
        result = analyze_with_gemini(title, watch_url, diff_text)
    except Exception as exc:
        logger.exception("Gemini Fehler:")
        await send_telegram(
            f"⚠️ <b>KI-Pause (Roh-Update sicherheitshalber):</b>\n"
            f"<b>{html.escape(title)}</b>\n{html.escape(watch_url)}\n\n"
            f"{html.escape(clean_diff[:600])}\n\n<i>Fehler: {html.escape(str(exc)[:150])}</i>"
        )
        return {"status": "gemini_fallback"}

    if not result.ist_neues_mietangebot or not result.passt_zu_suche:
        logger.info("Aussortiert: %s", result.ablehnungsgrund)
        return {"status": "filtered_out", "reason": result.ablehnungsgrund}

    fp_raw = f"{watch_url.lower()}|{result.objekt_titel.lower()}|{result.preis.lower()}|{result.groesse_zimmer.lower()}"
    fingerprint = hashlib.sha256(fp_raw.encode("utf-8")).hexdigest()

    if is_already_seen(fingerprint):
        logger.info("Duplikat unterdrückt: %s", result.objekt_titel)
        return {"status": "duplicate_ignored"}

    mark_as_seen(fingerprint, result.objekt_titel, watch_url)
    await send_listing_alert(title, watch_url, result)
    return {"status": "sent", "title": result.objekt_titel}
