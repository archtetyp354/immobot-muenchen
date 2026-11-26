import re

PHONE_CANDIDATE_RE = re.compile(
    r"(?:(?:\+|00)\s*49[\s/\-]*(?:\(0\)[\s/\-]*)?|0)(?:\d[\s/\-()]*){5,16}\d"
)
PHONE_LABEL_RE = re.compile(
    r"(?:telefon|tel\.?|fon|handy|mobil|phone)\s*[:.]?\s*([+\d][\d\s/\-()]{5,20}\d)",
    re.IGNORECASE,
)


def digits_only(raw: str) -> str:
    return re.sub(r"\D", "", raw or "")


def normalize_e164(raw: str) -> str:
    if not raw:
        return ""
    cleaned = raw.strip()
    cleaned = re.sub(r"\(0\)", "", cleaned)
    digits = digits_only(cleaned)
    if cleaned.strip().startswith("+") or cleaned.strip().startswith("00"):
        if digits.startswith("49"):
            national = digits[2:]
        else:
            return ""
    elif digits.startswith("0"):
        national = digits[1:]
    elif digits.startswith("49") and len(digits) >= 11:
        national = digits[2:]
    elif digits.startswith("89") and 8 <= len(digits) <= 12:
        national = digits
    else:
        return ""

    if national.startswith("0"):
        national = national[1:]
    if not (6 <= len(national) <= 13):
        return ""
    if national.startswith("89") and len(national) < 8:
        return ""
    compact = "+49" + national
    if not (10 <= len(digits_only(compact)) <= 15):
        return ""
    return compact


def format_phone(compact: str) -> str:
    if not compact.startswith("+49"):
        return compact
    national = compact[3:]
    if national.startswith("89"):
        return f"+49 89 {national[2:]}".strip()
    if national.startswith(("15", "16", "17")) and len(national) >= 4:
        return f"+49 {national[:3]} {national[3:]}".strip()
    return f"+49 {national}"


def extract_phones(text: str) -> list[str]:
    found: list[str] = []
    seen: set[str] = set()
    blobs = [m.group(1) for m in PHONE_LABEL_RE.finditer(text or "")]
    blobs.extend(m.group(0) for m in PHONE_CANDIDATE_RE.finditer(text or ""))
    for blob in blobs:
        compact = normalize_e164(blob)
        if compact and compact not in seen:
            seen.add(compact)
            found.append(compact)
    return found


def primary_phone(text: str) -> str:
    phones = extract_phones(text)
    return phones[0] if phones else ""
