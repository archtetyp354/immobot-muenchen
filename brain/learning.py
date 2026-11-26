import db


def create_inquiry(
    property_id: int,
    listing_id: int,
    fingerprint: str,
    recipient: str,
    subject: str,
    body: str,
    phone_e164: str = "",
    status: str = "scheduled",
) -> int:
    with db.connect() as conn:
        cur = conn.execute(
            """
            INSERT INTO inquiries (
                property_id, listing_id, fingerprint, recipient, subject, body, phone_e164, channel, status
            ) VALUES (?, ?, ?, ?, ?, ?, ?, 'email', ?)
            """,
            (property_id, listing_id, fingerprint, recipient, subject, body, phone_e164, status),
        )
        return cur.lastrowid or 0


def already_mailed(fingerprint: str, recipient: str, phone_e164: str = "") -> bool:
    with db.connect() as conn:
        row = conn.execute(
            """
            SELECT 1 FROM inquiries
            WHERE status IN ('scheduled', 'sending', 'sent', 'replied', 'invited')
              AND created_at >= datetime('now', '-14 days')
              AND (
                    fingerprint = ?
                    OR (recipient != '' AND lower(recipient) = lower(?) AND phone_e164 != '' AND phone_e164 = ?)
                  )
            LIMIT 1
            """,
            (fingerprint, recipient, phone_e164),
        ).fetchone()
        return row is not None


def claim_for_send(inquiry_id: int) -> tuple | None:
    with db.connect() as conn:
        conn.execute("BEGIN IMMEDIATE")
        row = conn.execute(
            """
            SELECT id, recipient, subject, body, fingerprint, property_id
            FROM inquiries WHERE id = ? AND status = 'scheduled'
            """,
            (inquiry_id,),
        ).fetchone()
        if not row:
            return None
        conn.execute("UPDATE inquiries SET status = 'sending' WHERE id = ?", (inquiry_id,))
        return row


def mark_sent(inquiry_id: int, message_id: str):
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE inquiries
            SET status = 'sent', smtp_message_id = ?, sent_at = CURRENT_TIMESTAMP
            WHERE id = ?
            """,
            (message_id, inquiry_id),
        )


def mark_failed(inquiry_id: int):
    with db.connect() as conn:
        conn.execute("UPDATE inquiries SET status = 'failed' WHERE id = ? AND status = 'sending'", (inquiry_id,))


def mark_cancelled(inquiry_id: int) -> bool:
    with db.connect() as conn:
        cur = conn.execute(
            "UPDATE inquiries SET status = 'cancelled' WHERE id = ? AND status = 'scheduled'",
            (inquiry_id,),
        )
        return cur.rowcount > 0


def expire_stale_scheduled():
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE inquiries
            SET status = 'cancelled'
            WHERE status = 'scheduled'
              AND created_at < datetime('now', '-3 minutes')
            """
        )


def find_inquiry_for_reply(from_addr: str, references: str) -> tuple | None:
    from_addr = (from_addr or "").lower()
    refs = references or ""
    with db.connect() as conn:
        rows = conn.execute(
            """
            SELECT id, smtp_message_id, recipient, status, subject
            FROM inquiries
            WHERE status IN ('sent', 'replied', 'invited')
              AND created_at >= datetime('now', '-45 days')
            ORDER BY id DESC
            LIMIT 80
            """
        ).fetchall()
    for row in rows:
        mid = row[1] or ""
        if mid and mid in refs:
            return row
        if from_addr and from_addr == (row[2] or "").lower():
            return row
    return None


def mark_reply(inquiry_id: int, invited: bool, note: str = ""):
    status = "invited" if invited else "replied"
    kind = "invitation" if invited else "reply"
    with db.connect() as conn:
        conn.execute(
            """
            UPDATE inquiries
            SET status = ?, replied_at = COALESCE(replied_at, CURRENT_TIMESTAMP)
            WHERE id = ?
            """,
            (status, inquiry_id),
        )
        conn.execute(
            "INSERT OR IGNORE INTO winning_inquiries (inquiry_id, kind, note) VALUES (?, ?, ?)",
            (inquiry_id, kind, note[:240]),
        )


def conversion_stats() -> dict:
    with db.connect() as conn:
        hits = conn.execute("SELECT COUNT(*) FROM seen_listings").fetchone()[0]
        sent = conn.execute(
            "SELECT COUNT(*) FROM inquiries WHERE status IN ('sent', 'replied', 'invited')"
        ).fetchone()[0]
        replies = conn.execute(
            "SELECT COUNT(*) FROM inquiries WHERE status IN ('replied', 'invited')"
        ).fetchone()[0]
        invites = conn.execute(
            "SELECT COUNT(*) FROM winning_inquiries WHERE kind = 'invitation'"
        ).fetchone()[0]
    def rate(num: int, den: int) -> str:
        if not den:
            return "–"
        return f"{round(100 * num / den)} %"
    return {
        "hits": hits,
        "sent": sent,
        "replies": replies,
        "invites": invites,
        "reply_rate": rate(replies, sent),
        "invite_rate": rate(invites, sent),
    }
