import re
from urllib.parse import urlparse

import db

PRICE_RE = re.compile(r"(\d{1,3}(?:[.\s]\d{3})+|\d{2,5})(?:[.,](\d{1,2}))?\s*(?:€|eur)", re.IGNORECASE)
SIZE_RE = re.compile(r"(\d{1,3}(?:[.,]\d{1,2})?)\s*(?:m²|m2|qm)\b", re.IGNORECASE)
ROOMS_RE = re.compile(r"(\d(?:[.,]\d)?)\s*(?:-|\s)?(?:zi\.?|zimmer)\b", re.IGNORECASE)
TOKEN_RE = re.compile(r"[a-z0-9äöüß]{3,}")


def parse_price_eur(text: str) -> int | None:
    match = PRICE_RE.search(text or "")
    if not match:
        return None
    whole = re.sub(r"[.\s]", "", match.group(1))
    try:
        return int(whole)
    except ValueError:
        return None


def parse_size_m2(text: str) -> float | None:
    match = SIZE_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def parse_rooms(text: str) -> float | None:
    match = ROOMS_RE.search(text or "")
    if not match:
        return None
    try:
        return float(match.group(1).replace(",", "."))
    except ValueError:
        return None


def _tokens(text: str) -> set[str]:
    return set(TOKEN_RE.findall((text or "").lower()))


def _jaccard(a: set[str], b: set[str]) -> float:
    if not a or not b:
        return 0.0
    return len(a & b) / len(a | b)


def _url_key(url: str) -> str:
    try:
        parsed = urlparse(url or "")
    except Exception:
        return ""
    path = parsed.path.rstrip("/").lower()
    host = parsed.netloc.lower().replace("www.", "")
    if not host:
        return ""
    return f"{host}{path}"


def _is_same(candidate: dict, row: tuple) -> bool:
    (
        _pid, _fp, _source, _sources, title, url, lage, _preis,
        price_eur, size_m2, rooms, phone, email,
    ) = row
    if candidate["fingerprint"] and candidate["fingerprint"] == _fp:
        return True
    if candidate["phone"] and phone and candidate["phone"] == phone:
        return True
    if candidate["url_key"] and candidate["url_key"] == _url_key(url or ""):
        return True

    price_close = (
        candidate["price_eur"] is not None
        and price_eur is not None
        and abs(candidate["price_eur"] - int(price_eur)) <= 40
    )
    size_close = (
        candidate["size_m2"] is not None
        and size_m2 is not None
        and abs(candidate["size_m2"] - float(size_m2)) <= 3
    )
    rooms_close = (
        candidate["rooms"] is not None
        and rooms is not None
        and abs(candidate["rooms"] - float(rooms)) <= 0.1
    )
    title_score = _jaccard(candidate["title_tokens"], _tokens(title or ""))
    lage_score = _jaccard(candidate["lage_tokens"], _tokens(lage or ""))
    same_email = bool(candidate["email"] and email and candidate["email"] == email.lower())

    if price_close and size_close and (title_score >= 0.34 or lage_score >= 0.5 or rooms_close):
        return True
    if price_close and rooms_close and (title_score >= 0.45 or lage_score >= 0.5):
        return True
    if same_email and price_close and title_score >= 0.3:
        return True
    if title_score >= 0.72 and (price_close or size_close or lage_score >= 0.5):
        return True
    return False


def register_listing(
    fingerprint: str,
    source: str,
    title: str,
    url: str,
    lage: str = "",
    preis: str = "",
    groesse: str = "",
    phone: str = "",
    email: str = "",
) -> tuple[bool, int]:
    blob = f"{title} {preis} {groesse} {lage}"
    candidate = {
        "fingerprint": fingerprint,
        "phone": phone,
        "email": (email or "").lower(),
        "url_key": _url_key(url),
        "price_eur": parse_price_eur(preis) or parse_price_eur(blob),
        "size_m2": parse_size_m2(groesse) or parse_size_m2(blob),
        "rooms": parse_rooms(groesse) or parse_rooms(title),
        "title_tokens": _tokens(title),
        "lage_tokens": _tokens(lage),
    }
    for row in db.recent_properties(14):
        if _is_same(candidate, row):
            db.note_property_source(row[0], source)
            return True, row[0]

    prop_id = db.insert_property({
        "fingerprint": fingerprint,
        "source": source,
        "sources": source,
        "title": title,
        "url": url,
        "lage": lage,
        "preis": preis,
        "price_eur": candidate["price_eur"],
        "size_m2": candidate["size_m2"],
        "rooms": candidate["rooms"],
        "phone_e164": phone,
        "email": (email or "").lower(),
    })
    return False, prop_id
