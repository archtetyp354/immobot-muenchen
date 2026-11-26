import argparse
import asyncio
import json
import logging
import math
import os
import re
import sqlite3
import time
from urllib.parse import urljoin, urlparse
from bs4 import BeautifulSoup
from curl_cffi.requests import AsyncSession as CffiSession
from google import genai
from google.genai import types
from pydantic import BaseModel, Field

logger = logging.getLogger("immobot-scout")

HBF_LAT = 48.1402
HBF_LON = 11.5583
RADIUS_M = 5000
OVERPASS_URL = "https://overpass-api.de/api/interpreter"

MODELS_FALLBACK = [
    "gemini-4-hydrogen",
    "gemini-3.5-flash-lite",
    "gemini-3.8-flash",
    "gemini-3.1-flash-lite",
]

EXCLUDED_DOMAINS = (
    "immobilienscout24", "immowelt", "kleinanzeigen", "wg-gesucht", "immonet",
    "ebay", "facebook", "instagram", "linkedin", "xing", "youtube", "wikipedia",
    "gelbeseiten", "dasoertliche", "11880", "google.", "bing.", "kununu",
    "stepstone", "brf-muenchen.de", "meinestadt", "mitula", "trovit",
    "nestoria", "remax.", "engelvoelkers.", "dahler", "vonovia", "sowon",
)

POSITIVE_LINK_WORDS = (
    "mietangebot", "mietwohnung", "freie-wohnung", "freie wohnung", "vermietung",
    "mieten", "wohnungsangebot", "objekte", "immobilien", "wohnraum", "angebote",
)
NEGATIVE_LINK_WORDS = (
    "impressum", "datenschutz", "karriere", "jobs", "kontakt", "agb",
    "kaufen", "verkauf", "weg-verwaltung", "eigentuemer", "eigentümer",
    "gewerbe", "referenz", "news", "blog", "login", "portal",
)
WIDGET_MARKERS = (
    "onoffice", "flowfact", "propstack", "immomio", "openimmo",
    "immobilio", "maklerserver", "lagom", "estate",
)
RENTAL_SIGNALS_RE = re.compile(
    r"(mietwohnung|mietangebot|vermietung|wohnungen|kaltmiete|warmmiete|zimmer|m²|qm|keine freien wohnungen|zurzeit keine|aktuell keine|interessenten|wohnungsbestand)",
    re.IGNORECASE,
)


def haversine_km(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371.0
    dlat = math.radians(lat2 - lat1)
    dlon = math.radians(lon2 - lon1)
    a = math.sin(dlat / 2) ** 2 + math.cos(math.radians(lat1)) * math.cos(math.radians(lat2)) * math.sin(dlon / 2) ** 2
    return round(2 * r * math.atan2(math.sqrt(a), math.sqrt(1 - a)), 2)


def clean_text(raw_html: str) -> str:
    txt = re.sub(r"<(script|style|noscript)[^>]*>.*?</\1>", " ", raw_html, flags=re.IGNORECASE | re.DOTALL)
    txt = re.sub(r"</(p|div|li|tr|h[1-6]|br)>", "\n", txt, flags=re.IGNORECASE)
    txt = re.sub(r"<[^>]+>", " ", txt)
    txt = re.sub(r"[ \t]+", " ", txt)
    return txt.strip()


def normalize_url(u: str) -> str:
    u = u.strip()
    if not u:
        return ""
    if not u.startswith(("http://", "https://")):
        u = "https://" + u
    return u


async def fetch_page(url: str) -> tuple[int, str, str]:
    try:
        async with CffiSession(impersonate="chrome", timeout=14) as session:
            resp = await session.get(url, allow_redirects=True)
            return resp.status_code, str(resp.url), resp.text or ""
    except Exception as exc:
        logger.debug("Fetch Fehler %s: %s", url, exc)
        return 0, url, ""


async def fetch_osm_munich_candidates(db_path: str | None = None) -> list[dict]:
    if db_path and os.path.exists(db_path):
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)")
                row = conn.execute("SELECT value FROM bot_state WHERE key = 'osm_cache_v1'").fetchone()
                ts_row = conn.execute("SELECT value FROM bot_state WHERE key = 'osm_cache_ts'").fetchone()
                if row and ts_row and (time.time() - float(ts_row[0]) < 14 * 86400):
                    return json.loads(row[0])
        except Exception:
            pass

    query = f"""
    [out:json][timeout:25];
    (
      nwr["office"="property_management"](around:{RADIUS_M},{HBF_LAT},{HBF_LON});
      nwr["office"="estate_agent"](around:{RADIUS_M},{HBF_LAT},{HBF_LON});
      nwr["name"~"Hausverwaltung|Baugenossenschaft|Wohnungsgenossenschaft|Wohnungsbau|Wohnungsstiftung|Bauverein|Siedlungswerk",i](around:{RADIUS_M},{HBF_LAT},{HBF_LON});
    );
    out center tags;
    """
    candidates = []
    seen_doms = set()
    try:
        async with CffiSession(impersonate="chrome", timeout=30) as session:
            resp = await session.post(
                OVERPASS_URL,
                data={"data": query},
                headers={"User-Agent": "ImmobotMunichPrivateScout/1.0"},
            )
            if resp.status_code != 200:
                logger.warning("Overpass Status %s", resp.status_code)
                return []
            elements = resp.json().get("elements", [])
            for el in elements:
                tags = el.get("tags") or {}
                site = tags.get("website") or tags.get("contact:website") or tags.get("url") or ""
                site = normalize_url(site)
                if not site:
                    continue
                dom = urlparse(site).netloc.lower().replace("www.", "")
                if not dom or dom in seen_doms or any(ex in dom for ex in EXCLUDED_DOMAINS):
                    continue
                seen_doms.add(dom)
                lat = el.get("lat") or (el.get("center") or {}).get("lat") or HBF_LAT
                lon = el.get("lon") or (el.get("center") or {}).get("lon") or HBF_LON
                dist = haversine_km(HBF_LAT, HBF_LON, float(lat), float(lon))
                name = tags.get("name") or dom
                office_type = tags.get("office") or "hausverwaltung"
                candidates.append({
                    "name": name,
                    "url": site,
                    "domain": dom,
                    "dist_km": dist,
                    "source": f"OSM ({office_type})",
                })
    except Exception as exc:
        logger.warning("Overpass Fehler: %s", exc)
        return []

    candidates.sort(key=lambda x: x["dist_km"])
    if db_path and candidates:
        try:
            with sqlite3.connect(db_path) as conn:
                conn.execute("CREATE TABLE IF NOT EXISTS bot_state (key TEXT PRIMARY KEY, value TEXT)")
                conn.execute("INSERT INTO bot_state (key, value) VALUES ('osm_cache_v1', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (json.dumps(candidates, ensure_ascii=False),))
                conn.execute("INSERT INTO bot_state (key, value) VALUES ('osm_cache_ts', ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value", (str(time.time()),))
        except Exception:
            pass
    return candidates


async def discover_subpage_and_tech(start_url: str) -> dict | None:
    status, final_url, home_html = await fetch_page(start_url)
    if status != 200 or len(home_html) < 200:
        return None

    low_home = home_html.lower()
    if any(m in low_home for m in ("just a moment", "cf-browser-verification", "captcha")):
        return None

    base_dom = urlparse(final_url).netloc.lower().replace("www.", "")
    soup = BeautifulSoup(home_html, "html.parser")

    scored_links: list[tuple[int, str]] = []
    seen_urls = {final_url.rstrip("/")}

    for a in soup.find_all("a", href=True):
        href = a["href"].strip()
        if href.startswith(("mailto:", "tel:", "javascript:", "#")):
            continue
        full = urljoin(final_url, href).split("#")[0]
        parsed = urlparse(full)
        dom = parsed.netloc.lower().replace("www.", "")
        if not dom or (dom != base_dom and not dom.endswith("." + base_dom)):
            continue
        if full.rstrip("/") in seen_urls or full.lower().endswith((".pdf", ".jpg", ".png")):
            continue

        txt = a.get_text(" ", strip=True).lower()
        combo = f"{parsed.path.lower()} {txt}"
        if any(neg in combo for neg in NEGATIVE_LINK_WORDS):
            continue

        score = 0
        for w in POSITIVE_LINK_WORDS:
            if w in parsed.path.lower():
                score += 6
            if w in txt:
                score += 4
        if score > 0:
            seen_urls.add(full.rstrip("/"))
            scored_links.append((score, full))

    scored_links.sort(key=lambda x: x[0], reverse=True)

    # Wenn kein guter Menü-Link gefunden wurde, typische Standardpfade nur testen, falls Startseite kurz/unspezifisch
    candidate_pages = [(10, final_url, home_html)]
    for sc, link in scored_links[:3]:
        st, f_url, sub_html = await fetch_page(link)
        if st == 200 and len(sub_html) > 200:
            candidate_pages.append((sc + 5, f_url, sub_html))

    if not scored_links:
        root = f"{urlparse(final_url).scheme}://{urlparse(final_url).netloc}"
        for guess in ("/mietangebote/", "/vermietung/", "/mietobjekte/"):
            g_url = root + guess
            if g_url.rstrip("/") in seen_urls:
                continue
            st, f_url, sub_html = await fetch_page(g_url)
            if st == 200 and len(sub_html) > 400 and RENTAL_SIGNALS_RE.search(sub_html):
                candidate_pages.append((8, f_url, sub_html))
                break

    # Beste Seite auswählen + iFrame/Widget-Analyse
    best_score = -1
    best_page = None
    for base_sc, page_url, raw_html in candidate_pages:
        vis = clean_text(raw_html)
        if not RENTAL_SIGNALS_RE.search(vis) and "iframe" not in raw_html.lower():
            continue
        psoup = BeautifulSoup(raw_html, "html.parser")
        iframe_urls = []
        for ifr in psoup.find_all("iframe", src=True):
            isrc = urljoin(page_url, ifr["src"].strip())
            if any(w in isrc.lower() for w in WIDGET_MARKERS) or "miete" in isrc.lower() or "immob" in isrc.lower():
                iframe_urls.append(isrc)

        has_widget = bool(iframe_urls) or any(m in raw_html.lower() for m in WIDGET_MARKERS)
        needs_browser = has_widget or len(vis) < 350

        # Wenn sauberes direktes iFrame existiert, kurz prüfen
        target_url = page_url
        if iframe_urls:
            ist, if_url, i_html = await fetch_page(iframe_urls[0])
            if ist == 200 and len(clean_text(i_html)) > 150:
                target_url = if_url
                vis = clean_text(i_html)
                needs_browser = False

        sc = base_sc + (4 if target_url != final_url else 0) + (3 if "miete" in target_url.lower() else 0)
        if sc > best_score:
            best_score = sc
            best_page = {
                "url": target_url,
                "parent_url": final_url,
                "fetch_backend": "html_webdriver" if needs_browser else "html_requests",
                "has_widget": has_widget,
                "visible_text": vis[:5000],
                "raw_html": raw_html,
            }

    return best_page


class ScoutEval(BaseModel):
    ist_geeignet: bool = Field(
        description="True NUR bei echtem Münchner Wohnungs-Vermieter (Hausverwaltung, Genossenschaft, Stiftung, kirchlich/Versicherung, kleiner Vermietungsmakler), wo Wohnungen <= 1.000 EUR realistisch vorkommen."
    )
    ausschlussgrund: str = Field(description="Falls False: z.B. 'Reine WEG-Verwaltung', 'Nur Gewerbe', 'Luxus-Bauträger', 'Tauschbörse'.")
    firmenname: str = Field(description="Kurzer sauberer Name des Anbieters")
    bezirk_hinweis: str = Field(description="Erkannter Stadtteil / Lage in München oder 'München Zentrum'")
    hat_leerstand_hinweis: bool = Field(description="True, wenn aktuell 0 Angebote/keine freien Wohnungen angezeigt werden (wichtiger Top-Kandidat für 24/7!)")
    empfohlenes_intervall_min: int = Field(description="8 bei dynamischen Verwaltern, 15 bei ruhigen Genossenschaften/Stiftungen")
    css_selektor_vorschlag: str = Field(description="Optional einfacher CSS-Selektor für den Wohnungsbereich, sonst leer.")
    confidence: float = Field(description="0.0 bis 1.0")
    kurzbegruendung: str


def evaluate_with_gemini(api_key: str, candidate: dict, page_info: dict, budget: int = 1000) -> ScoutEval:
    client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
    prompt = f"""
Du bist der Qualitäts-Türsteher für unsere Münchner Wohnungssuche (5 km um München Hauptbahnhof, Budget max. {budget} € Miete, 0 Tauschwohnungen).
Bewerte diese gefundene echte Website-Unterseite anhand ihres Live-Textes:

- Anbieter-Info: {candidate['name']} ({candidate['source']}, ca. {candidate['dist_km']} km vom Hbf)
- Gefundene Angebots-URL: {page_info['url']}
- Widget/JS erkannt: {page_info['has_widget']}

STRENGE BEWERTUNGSREGELN:
1. REINE WEG-VERWALTUNGEN (die nur Eigentümergemeinschaften verwalten, aber KEINE Mietwohnungen anbieten) -> ist_geeignet = false!
2. REINE GEWERBEVERWALTUNGEN (nur Büros/Läden) oder Luxus-Neubau-Verkauf -> ist_geeignet = false!
3. TAUSCHBÖRSEN -> ist_geeignet = false!
4. TOP-TREFFER (`ist_geeignet = true`): Klassische Hausverwaltungen mit Vermietungsseite, Genossenschaften, Bauvereine, Stiftungen, kirchliche Verwalter oder lokale Makler mit Mietwohnungen (auch wenn dort AKTUELL „keine freien Wohnungen“ steht – genau die sind Gold wert!).

LIVE-TEXT DER WEBSITE:
{page_info['visible_text'][:4500]}
"""
    last_exc = None
    for model_name in MODELS_FALLBACK:
        try:
            cfg = {"response_mime_type": "application/json", "response_schema": ScoutEval}
            if model_name == "gemini-4-hydrogen":
                cfg["thinking_config"] = types.ThinkingConfig(thinking_level="LOW")
            resp = client.models.generate_content(
                model=model_name,
                contents=prompt,
                config=types.GenerateContentConfig(**cfg),
            )
            return ScoutEval.model_validate_json(resp.text)
        except Exception as exc:
            last_exc = exc
            if any(c in str(exc) for c in ["429", "RESOURCE_EXHAUSTED", "503", "UNAVAILABLE", "500", "404"]):
                continue
            raise
    raise last_exc


async def google_search_extra_candidates(api_key: str, known_domains: set[str], district: str = "") -> list[dict]:
    fokus = f"im Stadtteil {district} München" if district else "im Münchner Zentrum (5 km um Hbf: Maxvorstadt, Sendling, Westend, Schwabing, Haidhausen, Neuhausen, Giesing)"
    prompt = (
        f"Finde per Google Search 6 reale, noch nicht gelistete kleine Münchner Hausverwaltungen, Wohnungsstiftungen oder Baugenossenschaften {fokus} mit eigener Website. "
        f"NICHT nennen: {', '.join(sorted(known_domains)[:35])}. Gib jeweils die exakte Website-URL an."
    )

    def _run():
        client = genai.Client(api_key=api_key, http_options=types.HttpOptions(api_version="v1beta"))
        for m in ("gemini-4-hydrogen", "gemini-3.8-flash", "gemini-3.5-flash-lite"):
            try:
                resp = client.models.generate_content(
                    model=m,
                    contents=prompt,
                    config=types.GenerateContentConfig(tools=[types.Tool(google_search=types.GoogleSearch())]),
                )
                urls = set(re.findall(r"https?://[^\s\"'<>)\]]+", resp.text or ""))
                meta = resp.candidates[0].grounding_metadata if resp.candidates else None
                if meta and meta.grounding_chunks:
                    for ch in meta.grounding_chunks:
                        if getattr(ch, "web", None) and getattr(ch.web, "uri", None):
                            urls.add(ch.web.uri)
                return list(urls)
            except Exception:
                continue
        return []

    raw_urls = await asyncio.to_thread(_run)
    extra = []
    for u in raw_urls:
        u = u.rstrip(".,;:")
        if "vertexaisearch.cloud.google.com" in u:
            continue
        dom = urlparse(u).netloc.lower().replace("www.", "")
        if not dom or dom in known_domains or any(ex in dom for ex in EXCLUDED_DOMAINS):
            continue
        known_domains.add(dom)
        extra.append({"name": dom, "url": u, "domain": dom, "dist_km": 3.0, "source": "Google Search KI"})
    return extra


async def run_scout_pipeline(
    api_key: str,
    known_domains: set[str],
    blacklist_check_fn=None,
    db_path: str | None = None,
    budget: int = 1000,
    district: str = "",
    max_probe: int = 14,
) -> list[dict]:
    osm_list = await fetch_osm_munich_candidates(db_path=db_path)
    extra_list = await google_search_extra_candidates(api_key, set(known_domains), district=district) if api_key else []

    combined = []
    seen_d = set(known_domains)
    for item in extra_list + osm_list:
        dom = item["domain"]
        if dom in seen_d:
            continue
        if blacklist_check_fn and blacklist_check_fn(item["url"]):
            continue
        seen_d.add(dom)
        combined.append(item)

    results = []
    probed = 0
    for cand in combined:
        if probed >= max_probe:
            break
        probed += 1
        page_info = await discover_subpage_and_tech(cand["url"])
        if not page_info:
            continue
        try:
            eval_res = await asyncio.to_thread(evaluate_with_gemini, api_key, cand, page_info, budget)
        except Exception as exc:
            logger.warning("Scout Gemini Fehler bei %s: %s", cand["domain"], exc)
            continue

        if not eval_res.ist_geeignet or eval_res.confidence < 0.72:
            results.append({"accepted": False, "candidate": cand, "page": page_info, "eval": eval_res})
            continue

        # CSS-Selektor live prüfen, damit Watch niemals blind wird
        valid_filters = []
        if eval_res.css_selektor_vorschlag and page_info["fetch_backend"] == "html_requests":
            try:
                soup = BeautifulSoup(page_info["raw_html"], "html.parser")
                els = soup.select(eval_res.css_selektor_vorschlag)
                if els and any(e.get_text(strip=True) for e in els):
                    valid_filters = [eval_res.css_selektor_vorschlag]
            except Exception:
                pass

        results.append({
            "accepted": True,
            "candidate": cand,
            "page": page_info,
            "eval": eval_res,
            "valid_filters": valid_filters,
        })
    return results


if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(message)s")
    parser = argparse.ArgumentParser(description="Immobot München Auto-Scout Test")
    parser.add_argument("--dry-run", action="store_true", help="Nur lokal testen und Ergebnisse anzeigen")
    parser.add_argument("--district", default="", help="Optionaler Stadtteil-Fokus")
    parser.add_argument("--limit", type=int, default=12, help="Anzahl zu prüfender Firmen")
    args = parser.parse_args()

    key = os.environ.get("GEMINI_API_KEY", "").strip()
    if not key or key.startswith("HIER_"):
        print("❌ GEMINI_API_KEY fehlt in .env!")
        raise SystemExit(1)

    print(f"🔭 Starte Münchner 5-km-Hausverwaltungs-Scout (Testlauf, max. {args.limit} Firmen)...")
    res = asyncio.run(run_scout_pipeline(
        api_key=key,
        known_domains=set(),
        db_path="/app/data/immobot.db" if os.path.isdir("/app/data") else None,
        district=args.district,
        max_probe=args.limit,
    ))

    accepted = [r for r in res if r["accepted"]]
    rejected = [r for r in res if not r["accepted"]]
    print(f"\n✅ GEEIGNET GEFUNDEN: {len(accepted)} | 🗑 Aussortiert (WEG/Gewerbe/usw.): {len(rejected)}\n")
    export_data = []
    for r in accepted:
        c, p, e = r["candidate"], r["page"], r["eval"]
        print(f"🏢 {e.firmenname} ({c['dist_km']} km zum Hbf | {c['source']})")
        print(f"   🔗 Angebots-URL: {p['url']}")
        print(f"   ⚙️ Technik: {p['fetch_backend']} | Intervall: {e.empfohlenes_intervall_min} Min. | Konfidenz: {e.confidence:.2f}")
        print(f"   💡 Grund: {e.kurzbegruendung}\n")
        export_data.append({
            "name": e.firmenname,
            "url": p["url"],
            "dist_km": c["dist_km"],
            "backend": p["fetch_backend"],
            "reason": e.kurzbegruendung,
        })

    out_path = "/app/data/discovered_muenchen.json" if os.path.isdir("/app/data") else "discovered_muenchen.json"
    with open(out_path, "w", encoding="utf-8") as f:
        json.dump(export_data, f, ensure_ascii=False, indent=2)
    print(f"💾 Liste gespeichert unter: {out_path}")
