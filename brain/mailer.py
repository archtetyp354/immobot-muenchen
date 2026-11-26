import asyncio
import html
import logging
import re
import smtplib
from email.message import EmailMessage
from email.utils import make_msgid

import db
import learning

logger = logging.getLogger("immobot-mailer")

COUNTDOWN_SEC = 120
EMAIL_RE = re.compile(r"[A-Z0-9._%+\-]+@[A-Z0-9.\-]+\.[A-Z]{2,}", re.IGNORECASE)
SKIP_LOCAL = ("noreply", "no-reply", "donotreply", "datenschutz", "webmaster", "abuse", "postmaster")
SKIP_HOSTS = ("example.com", "example.org", "changedetection.io", "sentry.io")

_pending: dict[int, asyncio.Task] = {}


def pick_recipient(text: str) -> str:
    for match in EMAIL_RE.finditer(text or ""):
        addr = match.group(0).strip().lower().rstrip(".")
        local, _, host = addr.partition("@")
        if not local or not host:
            continue
        if any(local.startswith(prefix) for prefix in SKIP_LOCAL):
            continue
        if host in SKIP_HOSTS or host.endswith(".png") or host.endswith(".jpg"):
            continue
        return addr
    return ""


def _smtp_send(recipient: str, subject: str, body: str, message_id: str):
    msg = EmailMessage()
    msg["From"] = db.SMTP_FROM
    msg["To"] = recipient
    msg["Subject"] = subject
    msg["Message-ID"] = message_id
    msg.set_content(body)
    if db.SMTP_PORT == 465:
        with smtplib.SMTP_SSL(db.SMTP_HOST, db.SMTP_PORT, timeout=30) as smtp:
            smtp.login(db.SMTP_USER, db.SMTP_PASSWORD)
            smtp.send_message(msg)
    else:
        with smtplib.SMTP(db.SMTP_HOST, db.SMTP_PORT, timeout=30) as smtp:
            smtp.ehlo()
            smtp.starttls()
            smtp.ehlo()
            smtp.login(db.SMTP_USER, db.SMTP_PASSWORD)
            smtp.send_message(msg)


async def _deliver(inquiry_id: int) -> str:
    row = learning.claim_for_send(inquiry_id)
    if not row:
        return "Schon weg oder abgebrochen."
    _iid, recipient, subject, body, _fp, _pid = row
    message_id = make_msgid(domain=(db.SMTP_FROM.split("@")[-1] or "immobot.local"))
    try:
        await asyncio.to_thread(_smtp_send, recipient, subject, body, message_id)
    except Exception as exc:
        learning.mark_failed(inquiry_id)
        logger.warning("SMTP-Fehler bei Anfrage %s: %s", inquiry_id, exc)
        await db.send_telegram(
            f"❌ <b>Bewerbung nicht raus:</b> {html.escape(recipient)}\n<i>{html.escape(str(exc)[:180])}</i>"
        )
        return "Versand fehlgeschlagen."
    learning.mark_sent(inquiry_id, message_id)
    await db.send_telegram(f"✉️ <b>Bewerbung raus</b> an {html.escape(recipient)}\n<i>{html.escape(subject[:120])}</i>")
    return "Gesendet."


async def _countdown(inquiry_id: int):
    try:
        await asyncio.sleep(COUNTDOWN_SEC)
        await _deliver(inquiry_id)
    except asyncio.CancelledError:
        return
    finally:
        _pending.pop(inquiry_id, None)


def recover():
    learning.expire_stale_scheduled()


async def consider_autopilot(
    listing_id: int,
    property_id: int,
    fingerprint: str,
    title: str,
    watch_url: str,
    anschreiben: str,
    recipient: str,
    phone_e164: str = "",
):
    mode = db.get_mail_mode()
    if mode == "off":
        return
    if not recipient:
        await db.send_telegram(
            f"✉️ <b>Keine Mailadresse</b> im Inserat von <b>{html.escape(title[:80])}</b>. Autopilot wartet."
        )
        return
    if not db.smtp_ready():
        await db.send_telegram("✉️ SMTP ist nicht gesetzt (SMTP_HOST, SMTP_USER, SMTP_PASSWORD, SMTP_FROM). Modus bleibt wirkungslos.")
        return
    if learning.already_mailed(fingerprint, recipient, phone_e164):
        await db.send_telegram(f"✉️ <b>Doppel-Schutz:</b> {html.escape(recipient)} hat diese Wohnung schon bekommen.")
        return

    subject = f"Mietanfrage: {title[:90]}"
    body = (anschreiben or "").strip() or f"Guten Tag,\n\nich interessiere mich für {title}.\n\n{watch_url}"
    inquiry_id = learning.create_inquiry(
        property_id=property_id,
        listing_id=listing_id,
        fingerprint=fingerprint,
        recipient=recipient,
        subject=subject,
        body=body,
        phone_e164=phone_e164,
        status="scheduled",
    )
    if mode == "auto":
        await _deliver(inquiry_id)
        return

    task = asyncio.create_task(_countdown(inquiry_id))
    _pending[inquiry_id] = task
    markup = {
        "inline_keyboard": [[
            {"text": "✉️ Jetzt senden", "callback_data": f"mnow:{inquiry_id}"},
            {"text": "Abbrechen", "callback_data": f"mcan:{inquiry_id}"},
        ]]
    }
    await db.send_telegram(
        f"✉️ <b>Bewerbung in {COUNTDOWN_SEC}s</b> an {html.escape(recipient)}\n"
        f"<i>{html.escape(subject)}</i>\n"
        f"🔗 {html.escape(watch_url)}",
        reply_markup=markup,
    )


async def send_now(inquiry_id: int) -> str:
    task = _pending.pop(inquiry_id, None)
    if task and not task.done():
        task.cancel()
    return await _deliver(inquiry_id)


async def cancel(inquiry_id: int) -> str:
    task = _pending.pop(inquiry_id, None)
    if task and not task.done():
        task.cancel()
    if learning.mark_cancelled(inquiry_id):
        await db.send_telegram("✉️ Bewerbung abgebrochen, nichts ist rausgegangen.")
        return "Abgebrochen."
    return "Zu spät, die Mail ist schon raus oder weg."
