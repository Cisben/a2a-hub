"""Public calls for contributions, separate from exclusive V3 assignments.

A call freezes the request. Each author submits once and receives an independent
requester decision. Events and submissions persist even when inbox hints expire.
"""
import json
import re
import time
import uuid

import artifacts
import messaging
from task_trust import Problem, authorize, encoded, idempotent, number, required_text, strings

MAX_OPEN_CALLS = 10
MAX_SUBMISSIONS = 100
UUID_PATTERN = r"[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}"
ROUTE = re.compile(r"/v1/calls(?:/(?P<call>" + UUID_PATTERN
                   + r")(?P<operation>/close|/submissions(?:/(?P<submission>" + UUID_PATTERN
                   + r")/(?P<decision>accept|reject))?)?)?")


def init(db):
    db.executescript("""
        CREATE TABLE IF NOT EXISTS calls(
            id TEXT PRIMARY KEY, poster TEXT NOT NULL, title TEXT NOT NULL,
            description TEXT NOT NULL, contract TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, expires REAL NOT NULL,
            closed_at TEXT);
        CREATE INDEX IF NOT EXISTS calls_poster_status ON calls(poster,status);
        CREATE TABLE IF NOT EXISTS call_submissions(
            id TEXT PRIMARY KEY, call_id TEXT NOT NULL, author TEXT NOT NULL,
            summary TEXT NOT NULL, artifacts TEXT NOT NULL, evidence TEXT NOT NULL,
            status TEXT NOT NULL, created_at TEXT NOT NULL, acceptance_due REAL NOT NULL,
            decided_at TEXT, reason TEXT, UNIQUE(call_id,author));
        CREATE INDEX IF NOT EXISTS submissions_call ON call_submissions(call_id,created_at,id);
        CREATE INDEX IF NOT EXISTS submissions_deadline ON call_submissions(status,acceptance_due);
        CREATE TABLE IF NOT EXISTS call_events(
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, call_id TEXT NOT NULL,
            submission_id TEXT, actor TEXT NOT NULL, kind TEXT NOT NULL,
            detail TEXT NOT NULL, created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS call_events_call ON call_events(call_id,sequence);
    """)


def event(db, hub, call_id, actor, kind, detail, submission_id=None):
    db.execute("INSERT INTO call_events(call_id,submission_id,actor,kind,detail,created_at) VALUES(?,?,?,?,?,?)",
               (call_id, submission_id, actor, kind, encoded(detail), hub.now_iso()))


def notify(db, hub, box, actor, kind, call_id, submission_id):
    """Best-effort inbox hint; the durable event remains the source of truth."""
    count = db.execute("SELECT COUNT(*) FROM messages WHERE box=? AND expires>?", (box, time.time())).fetchone()[0]
    if count >= messaging.MAX_PENDING:
        return False
    db.execute("""INSERT INTO messages
        (id,box,sender,mtype,body,created_at,expires,conversation_id,call_id,sender_verified)
        VALUES(?,?,?,?,?,?,?,?,?,1)""",
        (str(uuid.uuid4()), box, actor, kind, encoded({"call_id": call_id, "submission_id": submission_id}),
         hub.now_iso(), time.time() + messaging.DEFAULT_TTL_HOURS * 3600, call_id, call_id))
    return True


def expire(db, hub):
    now = time.time()
    for (call_id,) in db.execute("SELECT id FROM calls WHERE status='open' AND expires<=?", (now,)).fetchall():
        db.execute("UPDATE calls SET status='expired',closed_at=? WHERE id=?", (hub.now_iso(), call_id))
        event(db, hub, call_id, "system", "expired", {})
    rows = db.execute("SELECT id,call_id FROM call_submissions WHERE status='submitted' AND acceptance_due<=?", (now,)).fetchall()
    for submission_id, call_id in rows:
        db.execute("UPDATE call_submissions SET status='acceptance_expired',decided_at=? WHERE id=?", (hub.now_iso(), submission_id))
        event(db, hub, call_id, "system", "acceptance_expired", {}, submission_id)


def get_call(db, call_id):
    cursor = db.execute("SELECT * FROM calls WHERE id=?", (call_id,))
    row = cursor.fetchone()
    if not row:
        raise Problem(404, "unknown_call", "call does not exist")
    result = dict(zip((column[0] for column in cursor.description), row))
    result["call_id"] = result.pop("id")
    result["contract"] = json.loads(result["contract"])
    return result


def create(db, hub, body, actor):
    title = required_text(body, "title", 140)
    description = required_text(body, "description", 4000)
    contract = {"acceptance_criteria": strings(body, "acceptance_criteria", True),
                "constraints": strings(body, "constraints"),
                "acceptance_hours": number(body, "acceptance_hours", 72, 168)}
    ttl = number(body, "ttl_hours", 168, 720)
    if db.execute("SELECT COUNT(*) FROM calls WHERE poster=? AND status='open'", (actor,)).fetchone()[0] >= MAX_OPEN_CALLS:
        raise Problem(429, "too_many_calls", "at most 10 open calls per requester")
    call_id = str(uuid.uuid4())
    db.execute("INSERT INTO calls(id,poster,title,description,contract,status,created_at,expires) VALUES(?,?,?,?,?,'open',?,?)",
               (call_id, actor, title, description, encoded(contract), hub.now_iso(), time.time() + ttl * 3600))
    event(db, hub, call_id, actor, "open", {"contract": contract})
    return get_call(db, call_id)


def contribute(db, hub, call, body, actor):
    if call["status"] != "open":
        raise Problem(409, "call_closed", "call no longer accepts submissions")
    if actor == call["poster"]:
        raise Problem(400, "self_submission", "requesters cannot submit to their own calls")
    call_id = call["call_id"]
    if db.execute("SELECT 1 FROM call_submissions WHERE call_id=? AND author=?", (call_id, actor)).fetchone():
        raise Problem(409, "already_submitted", "one submission per author; revisions require a new call")
    if db.execute("SELECT COUNT(*) FROM call_submissions WHERE call_id=?", (call_id,)).fetchone()[0] >= MAX_SUBMISSIONS:
        raise Problem(429, "too_many_submissions", "call has reached its 100-submission limit")
    summary = required_text(body, "summary", 4000)
    references = artifacts.validate(body.get("artifacts", []), required=True)
    evidence = strings(body, "evidence", True)
    submission_id = str(uuid.uuid4())
    due = time.time() + call["contract"]["acceptance_hours"] * 3600
    db.execute("""INSERT INTO call_submissions
        (id,call_id,author,summary,artifacts,evidence,status,created_at,acceptance_due)
        VALUES(?,?,?,?,?,?,'submitted',?,?)""",
        (submission_id, call_id, actor, summary, encoded(references), encoded(evidence), hub.now_iso(), due))
    queued = notify(db, hub, call["poster"], actor, "call_submission", call_id, submission_id)
    event(db, hub, call_id, actor, "submitted", {"inbox_notification_queued": queued}, submission_id)
    return {"call_id": call_id, "submission_id": submission_id, "status": "submitted", "acceptance_due": due}


def mutate(db, hub, route, body, actor):
    call_id, operation = route["call"], route["operation"]
    if not call_id:
        return create(db, hub, body, actor)
    call = get_call(db, call_id)
    if operation == "/submissions":
        return contribute(db, hub, call, body, actor)
    if actor != call["poster"]:
        raise Problem(403, "wrong_actor", "only the requester can close or decide submissions")
    reason = required_text(body, "reason", 2000)
    if operation == "/close":
        if call["status"] != "open":
            raise Problem(409, "call_closed", "call is already closed or expired")
        db.execute("UPDATE calls SET status='closed',closed_at=? WHERE id=?", (hub.now_iso(), call_id))
        event(db, hub, call_id, actor, "closed", {"reason": reason})
        return {"call_id": call_id, "status": "closed"}
    submission_id = route["submission"]
    row = db.execute("SELECT author,status FROM call_submissions WHERE id=? AND call_id=?", (submission_id, call_id)).fetchone()
    if not row:
        raise Problem(404, "unknown_submission", "submission does not belong to this call")
    author, status = row
    if status != "submitted":
        raise Problem(409, "wrong_status", "submission already decided or acceptance window expired")
    target = "accepted" if route["decision"] == "accept" else "rejected"
    db.execute("UPDATE call_submissions SET status=?,decided_at=?,reason=? WHERE id=?",
               (target, hub.now_iso(), reason, submission_id))
    queued = notify(db, hub, author, actor, "call_" + target, call_id, submission_id)
    event(db, hub, call_id, actor, target, {"reason": reason, "inbox_notification_queued": queued}, submission_id)
    return {"call_id": call_id, "submission_id": submission_id, "status": target, "reason": reason}


def read(db, route, query):
    call_id = route["call"]
    if not call_id:
        status = query.get("status", ["open"])[0]
        if status not in ("open", "closed", "expired", "all"):
            raise Problem(400, "invalid_status", "status must be open, closed, expired or all")
        try:
            cursor = int(query.get("cursor", ["0"])[0])
            limit = int(query.get("limit", ["50"])[0])
        except ValueError:
            raise Problem(400, "invalid_query", "cursor and limit must be integers")
        if not 0 <= cursor <= messaging.MAX_CURSOR or not 1 <= limit <= 100:
            raise Problem(400, "invalid_query", "invalid cursor or limit")
        # The immutable creation event is a durable enumeration cursor.
        clauses = "e.kind='open' AND e.sequence>?"
        params = [cursor]
        if status != "all":
            clauses += " AND c.status=?"
            params.append(status)
        rows = db.execute("SELECT c.id,e.sequence FROM calls c JOIN call_events e ON c.id=e.call_id WHERE "
                          + clauses + " ORDER BY e.sequence LIMIT ?", params + [limit + 1]).fetchall()
        page = rows[:limit]
        return {"calls": [get_call(db, item[0]) for item in page], "count": len(page),
                "next_cursor": page[-1][1] if page else cursor, "has_more": len(rows) > limit}
    call = get_call(db, call_id)
    cursor = db.execute("SELECT * FROM call_submissions WHERE call_id=? ORDER BY created_at,id", (call_id,))
    submissions = []
    for row in cursor.fetchall():
        item = dict(zip((column[0] for column in cursor.description), row))
        item["submission_id"] = item.pop("id")
        for key in ("artifacts", "evidence"):
            item[key] = json.loads(item[key])
        submissions.append(item)
    if route["operation"] == "/submissions":
        return {"call_id": call_id, "submissions": submissions, "count": len(submissions)}
    call["submissions"] = submissions
    call["events"] = [dict(zip(("cursor", "submission_id", "actor", "kind", "detail", "created_at"), row))
                      for row in db.execute("SELECT sequence,submission_id,actor,kind,detail,created_at FROM call_events WHERE call_id=? ORDER BY sequence", (call_id,))]
    for item in call["events"]:
        item["detail"] = json.loads(item["detail"])
    call["decision_basis"] = "Requester decisions, not independent certification; artifact metadata is publisher-declared."
    return call


def dispatch(handler, hub, method, path, query):
    route = ROUTE.fullmatch(path)
    if not route:
        return False
    operation = route["operation"]
    can_read = operation in (None, "/submissions")
    can_write = not route["call"] or operation is not None
    if (method == "GET" and not can_read) or (method == "POST" and not can_write) or method not in ("GET", "POST"):
        raise Problem(405, "method_not_allowed", "unsupported call operation")
    body = handler.read_json() if method == "POST" else None
    with hub.DB_LOCK:
        with hub.DB:
            expire(hub.DB, hub)
        if method == "GET":
            response = read(hub.DB, route, query)
        else:
            actor = body.get("author" if operation == "/submissions" else "poster")
            authorize(hub.DB, handler.headers, actor)
            response = idempotent(hub.DB, handler.headers, method, path, body, actor,
                                  lambda: mutate(hub.DB, hub, route, body, actor))
    hub.notify_cond()
    handler.send_json(response)
    return True
