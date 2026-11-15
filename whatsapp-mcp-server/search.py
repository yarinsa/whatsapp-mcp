"""Full-text message search over messages.db.

The index is an FTS5 trigram table (`messages_fts`) whose rowids point at
`messages.rowid`. It is maintained here, not by the bridge: before each
search, rows added since the last indexed rowid are indexed. Trigram matching
is substring matching, so Hebrew words with attached prefixes (ו/ה/ב/ל/ש)
still match, and it is case-insensitive.
"""
import re
import sqlite3
from datetime import datetime
from typing import Dict, List, Optional, Set

from whatsapp import connect, get_sender_name, normalize_name, user_part

SNIPPET_CHARS = 160


def ensure_index(conn: sqlite3.Connection) -> int:
    """Create the index if needed and add new messages. Returns rows added."""
    conn.executescript("""
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts
            USING fts5(content, tokenize='trigram');
        CREATE TABLE IF NOT EXISTS search_meta (key TEXT PRIMARY KEY, value INTEGER);
        CREATE INDEX IF NOT EXISTS idx_messages_ts ON messages(timestamp);
        CREATE INDEX IF NOT EXISTS idx_messages_sender ON messages(sender);
    """)
    row = conn.execute("SELECT value FROM search_meta WHERE key = 'last_rowid'").fetchone()
    last = row[0] if row else 0
    top = conn.execute("SELECT COALESCE(MAX(rowid), 0) FROM messages").fetchone()[0]
    if top <= last:
        return 0
    with conn:
        cur = conn.execute(
            """INSERT INTO messages_fts(rowid, content)
               SELECT rowid, content FROM messages
               WHERE rowid > ? AND content IS NOT NULL AND content != ''""",
            (last,),
        )
        conn.execute(
            "INSERT OR REPLACE INTO search_meta(key, value) VALUES ('last_rowid', ?)", (top,)
        )
    return cur.rowcount


def sender_variants(conn: sqlite3.Connection, who: str) -> Set[str]:
    """All stored sender spellings for a person given a phone, LID, JID or name.

    Messages from one person can be stored under their phone, their LID, or a
    full JID of either, so an exact match on one form misses the rest.
    """
    users: Set[str] = set()
    digits = re.sub(r"\D", "", user_part(who))
    if digits and len(digits) >= 6:
        users.add(digits)
        rows = conn.execute(
            "SELECT phone, lid FROM contacts WHERE user = ? OR phone = ? OR lid = ?",
            (digits, digits, digits),
        ).fetchall()
    else:
        p = f"%{normalize_name(who)}%"
        rows = conn.execute(
            """SELECT phone, lid FROM contacts
               WHERE norm(name) LIKE ? OR norm(full_name) LIKE ? OR norm(push_name) LIKE ?""",
            (p, p, p),
        ).fetchall()
    for phone, lid in rows:
        users.update(u for u in (phone, lid) if u)
    # A person's phone and LID are linked through any contacts row carrying both.
    if users:
        marks = ",".join("?" * len(users))
        for phone, lid in conn.execute(
            f"SELECT phone, lid FROM contacts WHERE phone IN ({marks}) OR lid IN ({marks})",
            (*users, *users),
        ).fetchall():
            users.update(u for u in (phone, lid) if u)
    out: Set[str] = set()
    for u in users:
        out.update({u, f"{u}@s.whatsapp.net", f"{u}@lid"})
    return out


def sender_clause(conn: sqlite3.Connection, who: str, params: list) -> str:
    variants = sender_variants(conn, who)
    if not variants:
        return "0"
    params.extend(sorted(variants))
    return f"m.sender IN ({','.join('?' * len(variants))})"


def _date(value: str, name: str) -> str:
    try:
        return datetime.fromisoformat(value).isoformat()
    except ValueError:
        raise ValueError(f"Invalid date for '{name}': {value}. Use ISO-8601, e.g. 2026-11-01.")


def _snippet(text: str, words: List[str]) -> str:
    text = " ".join((text or "").split())
    if len(text) <= SNIPPET_CHARS:
        return text
    low = text.casefold()
    hits = [low.find(w.casefold()) for w in words if low.find(w.casefold()) >= 0]
    start = max(0, min(hits) - 50) if hits else 0
    end = start + SNIPPET_CHARS
    return ("…" if start else "") + text[start:end] + ("…" if end < len(text) else "")


def search_messages(
    query: Optional[str] = None,
    chat_jid: Optional[str] = None,
    sender: Optional[str] = None,
    after: Optional[str] = None,
    before: Optional[str] = None,
    from_me: Optional[bool] = None,
    media_type: Optional[str] = None,
    sort: str = "recent",
    limit: int = 30,
    page: int = 0,
) -> str:
    """Search messages; returns one compact line per hit, newest first by default."""
    words = [w for w in (query or "").split() if w]
    conn = connect()
    try:
        ensure_index(conn)
        where, params = [], []
        fts_words = [w for w in words if len(w) >= 3]
        short_words = [w for w in words if len(w) < 3]
        join = ""
        if fts_words:
            join = "JOIN messages_fts f ON f.rowid = m.rowid"
            where.append("messages_fts MATCH ?")
            params.append(" AND ".join('"' + w.replace('"', '""') + '"' for w in fts_words))
        for w in short_words:
            where.append("m.content LIKE ?")
            params.append(f"%{w}%")
        if chat_jid:
            where.append("m.chat_jid = ?")
            params.append(chat_jid)
        if sender:
            where.append(sender_clause(conn, sender, params))
        if after:
            where.append("julianday(m.timestamp) >= julianday(?)")
            params.append(_date(after, "after"))
        if before:
            where.append("julianday(m.timestamp) < julianday(?)")
            params.append(_date(before, "before"))
        if from_me is not None:
            where.append("m.is_from_me = ?")
            params.append(1 if from_me else 0)
        if media_type:
            where.append("m.media_type = ?")
            params.append(media_type)
        if not where:
            raise ValueError("Give a query or at least one filter.")

        order = "f.rank" if sort == "relevance" and fts_words else "julianday(m.timestamp) DESC"
        sql = f"""
            SELECT m.id, m.chat_jid, c.name, m.sender, m.is_from_me, m.timestamp,
                   m.content, m.media_type
            FROM messages m {join}
            LEFT JOIN chats c ON c.jid = m.chat_jid
            WHERE {' AND '.join(where)}
            ORDER BY {order} LIMIT ? OFFSET ?"""
        params.extend([limit + 1, page * limit])
        rows = conn.execute(sql, params).fetchall()
    finally:
        conn.close()

    more = len(rows) > limit
    rows = rows[:limit]
    if not rows:
        return "No messages found."
    names: Dict[str, str] = {}
    lines = []
    for mid, chat, chat_name, snd, mine, ts, content, media in rows:
        who = "Me" if mine else names.setdefault(snd, get_sender_name(snd))
        when = datetime.fromisoformat(ts).strftime("%Y-%m-%d %H:%M")
        where_ = chat_name or chat
        text = _snippet(content, words)
        if media:
            text = f"[{media}] {text}".strip()
        lines.append(f"[{when}] {where_} | {who}: {text}  (id={mid} chat={chat})")
    if more:
        lines.append(f"… more results: page={page + 1}")
    return "\n".join(lines)
