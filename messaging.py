"""Ephemeral inbox storage with explicit limits and resumable delivery.

All functions touching SQLite require the caller's database lock. Writes belong
to the caller's transaction; no function here commits or performs network I/O.
"""
import hashlib
import json
import secrets
import time
import uuid

import artifacts
from task_trust import NAME, Problem, encoded, number

MAX_BODY_CHARS = 8192
MAX_PENDING = 200
DEFAULT_TTL_HOURS = 72
MAX_TTL_HOURS = 168
MAX_CURSOR = 2**63 - 1


def init(db, add_column):
    for name, definition in (
        ("conversation_id", "TEXT"), ("reply_to", "TEXT"),
        ("job_id", "TEXT"), ("call_id", "TEXT"),
        ("sender_verified", "INTEGER NOT NULL DEFAULT 0"),
        ("artifacts", "TEXT NOT NULL DEFAULT '[]'"),
    ):
        add_column(db, "messages", name, definition)
    db.executescript("""
        CREATE TABLE IF NOT EXISTS message_cursors(
            sequence INTEGER PRIMARY KEY AUTOINCREMENT,
            message_id TEXT NOT NULL UNIQUE);
        INSERT INTO message_cursors(message_id)
            SELECT id FROM messages WHERE id NOT IN (SELECT message_id FROM message_cursors)
            ORDER BY created_at, id;
        CREATE TRIGGER IF NOT EXISTS message_cursor_insert AFTER INSERT ON messages
        BEGIN
            INSERT INTO message_cursors(message_id) VALUES(NEW.id);
        END;
        CREATE TRIGGER IF NOT EXISTS message_cursor_delete AFTER DELETE ON messages
        BEGIN
            DELETE FROM message_cursors WHERE message_id=OLD.id;
        END;
        CREATE INDEX IF NOT EXISTS messages_conversation ON messages(box, conversation_id);
    """)


def box_key_ok(db, box, supplied):
    row = db.execute("SELECT key_hash FROM boxes WHERE box=?", (box,)).fetchone()
    if not row or not row[0]:
        return True
    return bool(supplied) and secrets.compare_digest(
        hashlib.sha256(str(supplied).encode()).hexdigest(), row[0])


def optional_name(body, name):
    value = body.get(name)
    if value is not None and (not isinstance(value, str) or not NAME.fullmatch(value)):
        raise Problem(400, "invalid_field", name + " must contain 1-64 safe ASCII characters")
    return value


def enqueue(db, box, body, verified, now_iso, reply_key=None):
    text = body.get("body")
    if not isinstance(text, str) or not text.strip():
        raise Problem(400, "invalid_body", "body must be a nonempty string")
    if len(text) > MAX_BODY_CHARS:
        raise Problem(413, "message_too_large", "body exceeds 8192 Unicode characters; use artifacts for HTTPS file references")
    sender = body.get("sender", "anonymous")
    if not isinstance(sender, str) or not sender.strip() or len(sender) > 64:
        raise Problem(400, "invalid_sender", "sender must contain 1-64 characters")
    message_type = body.get("type", "note")
    if not isinstance(message_type, str) or not NAME.fullmatch(message_type):
        raise Problem(400, "invalid_type", "type must contain 1-64 safe ASCII characters")
    ttl = number(body, "ttl_hours", DEFAULT_TTL_HOURS, MAX_TTL_HOURS)
    references = artifacts.validate(body.get("artifacts", []))
    conversation = optional_name(body, "conversation_id")
    reply_to = optional_name(body, "reply_to")
    job_id = optional_name(body, "job_id")
    call_id = optional_name(body, "call_id")
    if reply_to:
        parent = db.execute(
            "SELECT box,conversation_id,job_id,call_id FROM messages WHERE id=? AND expires>?",
            (reply_to, time.time())).fetchone()
        if not parent or not box_key_ok(db, parent[0], reply_key):
            raise Problem(404, "reply_unavailable", "reply target is missing, expired or inaccessible")
        parent_conversation = parent[1] or reply_to
        for value, parent_value in ((conversation, parent_conversation), (job_id, parent[2]), (call_id, parent[3])):
            if value and parent_value and value != parent_value:
                raise Problem(409, "reply_context_conflict", "reply context differs from its parent")
        conversation, job_id, call_id = parent_conversation, job_id or parent[2], call_id or parent[3]
    if job_id and call_id:
        raise Problem(400, "ambiguous_context", "associate a message with either job_id or call_id")
    for value, table, label in ((job_id, "jobs", "job"), (call_id, "calls", "call")):
        if value and not db.execute("SELECT 1 FROM " + table + " WHERE id=?", (value,)).fetchone():
            raise Problem(404, "unknown_" + label, label + " does not exist")
    key = body.get("key")
    if key is not None and (not isinstance(key, str) or not 1 <= len(key) <= 256):
        raise Problem(400, "invalid_key", "key must contain 1-256 characters")
    pending = db.execute("SELECT COUNT(*) FROM messages WHERE box=? AND expires>?", (box, time.time())).fetchone()[0]
    if pending >= MAX_PENDING:
        raise Problem(429, "inbox_full", "inbox has 200 unacknowledged messages; retry after consumption or expiry")
    if not db.execute("SELECT 1 FROM boxes WHERE box=?", (box,)).fetchone():
        db.execute("INSERT INTO boxes(box,key_hash,created_at,created_by) VALUES(?,?,?,?)",
                   (box, hashlib.sha256(key.encode()).hexdigest() if key else None, now_iso(), sender))
    message_id = str(uuid.uuid4())
    conversation = conversation or message_id
    created = now_iso()
    db.execute("""INSERT INTO messages
        (id,box,sender,mtype,body,created_at,expires,conversation_id,reply_to,job_id,call_id,sender_verified,artifacts)
        VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?)""",
        (message_id, box, sender, message_type, text, created, time.time() + ttl * 3600,
         conversation, reply_to, job_id, call_id, int(verified), encoded(references)))
    db.execute("INSERT INTO counters(k,v) VALUES('messages_relayed',1) ON CONFLICT(k) DO UPDATE SET v=v+1")
    return {"queued": message_id, "box": box, "created_at": created,
            "expires_hours": ttl, "conversation_id": conversation, "sender_verified": bool(verified),
            "note": "Reading does not acknowledge. Artifact metadata is publisher-declared."}


def query_options(query):
    def integer(name, default, minimum, maximum):
        try:
            value = int(query.get(name, [str(default)])[0])
        except (ValueError, TypeError):
            raise Problem(400, "invalid_query", name + " must be an integer")
        if not minimum <= value <= maximum:
            raise Problem(400, "invalid_query", name + " is outside its supported range")
        return value
    limit = integer("limit", 50, 1, 100)
    cursor = integer("cursor", 0, 0, MAX_CURSOR) if "cursor" in query else None
    if cursor is not None and "after" in query:
        raise Problem(400, "invalid_query", "cursor and after cannot be combined")
    try:
        wait = float(query.get("wait", ["0"])[0])
    except ValueError:
        raise Problem(400, "invalid_query", "wait must be 0-55 seconds")
    if not 0 <= wait <= 55:
        raise Problem(400, "invalid_query", "wait must be 0-55 seconds")
    conversation = optional_name({"conversation_id": query.get("conversation_id", [None])[0]}, "conversation_id")
    return {"limit": limit, "cursor": cursor, "after": query.get("after", [None])[0],
            "wait": wait, "conversation": conversation}


def read(db, box, options):
    clauses = ["m.box=?", "m.expires>?"]
    parameters = [box, time.time()]
    for value, clause in ((options["cursor"], "c.sequence>?"),
                          (options["after"], "m.created_at>?"),
                          (options["conversation"], "COALESCE(m.conversation_id,m.id)=?")):
        if value is not None:
            clauses.append(clause)
            parameters.append(value)
    order = "c.sequence ASC" if options["cursor"] is not None else "c.sequence DESC"
    parameters.append(options["limit"] + 1)
    rows = db.execute("""SELECT m.id,m.sender,m.mtype,m.body,m.created_at,
        COALESCE(m.conversation_id,m.id),m.reply_to,m.job_id,m.call_id,m.sender_verified,m.artifacts,c.sequence
        FROM messages m JOIN message_cursors c ON c.message_id=m.id WHERE """
        + " AND ".join(clauses) + " ORDER BY " + order + " LIMIT ?", parameters).fetchall()
    messages = []
    keys = ("id", "sender", "type", "body", "created_at", "conversation_id", "reply_to", "job_id", "call_id", "sender_verified", "artifacts", "cursor")
    for row in rows[:options["limit"]]:
        message = dict(zip(keys, row))
        message["sender_verified"] = bool(message["sender_verified"])
        message["artifacts"] = json.loads(message["artifacts"])
        messages.append(message)
    result = {"box": box, "count": len(messages), "messages": messages,
              "count_basis": "unexpired messages not acknowledged; reading does not acknowledge",
              "has_more": len(rows) > options["limit"],
              "hint": "Use cursor=0 for incremental reads; explicitly POST /ack to consume."}
    if options["cursor"] is not None:
        result["next_cursor"] = messages[-1]["cursor"] if messages else options["cursor"]
    return result
