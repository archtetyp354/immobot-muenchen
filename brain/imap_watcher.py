import asyncio
import email
import html
import imaplib
import logging
import re
from email.header import decode_header

import db
import learning

logger = logging.getLogger("immobot-imap")

INVITE_RE = re.compile(
    r"(besichtigung|einladung|besichtigen|termin(?:vorschlag)?|vorbeikommen|schlüsselübergabe|wohnung ansehen)",
    re.IGNORECASE,
)


def _decode_header(value: str) -> str:
    if not value:
        return ""
    parts = []
    for blob, charset in decode_header(value):
        if isinstance(blob, bytes):
            parts.append(blob.decode(charset or "utf-8", errors="replace"))
        else:
            parts.append(blob)
    return "".join(parts)


def _body_text(msg: email.message.Message) -> str:
    chunks = []
    if msg.is_multipart():
        for part in msg.walk():
            if part.get_content_type() != "text/plain":
                continue
            payload = part.get_payload(decode=True) or b""
            charset = part.get_content_charset() or "utf-8"
            chunks.append(payload.decode(charset, errors="replace"))
    else:
        payload = msg.get_payload(decode=True) or b""
        charset = msg.get_content_charset() or "utf-8"
        chunks.append(payload.decode(charset, errors="replace"))
    return "\n".join(chunks)


def _max_uid(client: imaplib.IMAP4_SSL) -> str:
    typ, data = client.uid("SEARCH", None, "ALL")
    if typ != "OK" or not data or not data[0]:
        return "0"
    uids = data[0].split()
    return uids[-1].decode() if uids else "0"


def check_once() -> list[dict]:
    client = imaplib.IMAP4_SSL(db.IMAP_HOST, db.IMAP_PORT)
    try:
        client.login(db.IMAP_USER, db.IMAP_PASSWORD)
        client.select("INBOX", readonly=True)
        last_uid = db.get_state("imap_last_uid", "")
        if not last_uid:
            latest = _max_uid(client)
            db.set_state("imap_last_uid", latest or "0")
            return []
        typ, data = client.uid("SEARCH", None, "UID", f"{int(last_uid) + 1}:*")
        if typ != "OK" or not data or not data[0]:
            return []
        found = []
        max_seen = int(last_uid)
        for raw_uid in data[0].split():
            uid = raw_uid.decode()
            max_seen = max(max_seen, int(uid))
            ftyp, fetched = client.uid("FETCH", uid, "(BODY.PEEK[])")
            if ftyp != "OK" or not fetched:
                continue
            raw_msg = b""
            for item in fetched:
                if isinstance(item, tuple) and len(item) >= 2 and isinstance(item[1], (bytes, bytearray)):
                    raw_msg = bytes(item[1])
                    break
            if not raw_msg:
                continue
            msg = email.message_from_bytes(raw_msg)
            from_addr = email.utils.parseaddr(_decode_header(msg.get("From", "")))[1].lower()
            subject = _decode_header(msg.get("Subject", ""))
            references = " ".join(filter(None, [
                msg.get("In-Reply-To", ""),
                msg.get("References", ""),
                msg.get("Message-ID", ""),
            ]))
            body = _body_text(msg)
            inquiry = learning.find_inquiry_for_reply(from_addr, references)
            if not inquiry:
                continue
            invited = bool(INVITE_RE.search(f"{subject}\n{body[:1500]}"))
            learning.mark_reply(inquiry[0], invited=invited, note=subject)
            found.append({
                "from": from_addr,
                "subject": subject,
                "snippet": re.sub(r"\s+", " ", body).strip()[:280],
                "invited": invited,
            })
        db.set_state("imap_last_uid", str(max_seen))
        return found
    finally:
        try:
            client.logout()
        except Exception:
            pass


async def imap_poll_loop():
    await asyncio.sleep(25)
    while True:
        try:
            if db.imap_ready():
                events = await asyncio.to_thread(check_once)
                mute = db.get_state("mute_errors", "0") == "1"
                for event in events:
                    if event["invited"]:
                        await db.send_telegram(
                            f"📬 <b>VIP-Einladung</b> von {html.escape(event['from'])}\n"
                            f"<b>{html.escape(event['subject'][:140])}</b>\n"
                            f"<i>{html.escape(event['snippet'])}</i>"
                        )
                    elif not mute:
                        await db.send_telegram(
                            f"📬 Antwort von {html.escape(event['from'])}\n"
                            f"<b>{html.escape(event['subject'][:140])}</b>\n"
                            f"<i>{html.escape(event['snippet'])}</i>"
                        )
        except asyncio.CancelledError:
            break
        except Exception as exc:
            logger.warning("IMAP-Check: %s", exc)
        await asyncio.sleep(120)
