"""Authenticated task contracts and atomic lifecycle receipts (stdlib only)."""

import hashlib
import json
import math
import re
import secrets
import time
import uuid


STATES = ["open", "claimed", "submitted", "accepted", "rejected", "failed",
          "expired", "acceptance_expired", "done"]
NAME = re.compile(r"[A-Za-z0-9_.-]{1,64}\Z")


class Problem(Exception):
    def __init__(self, status, code, message):
        self.status, self.code, self.message = status, code, message


def encoded(value):
    return json.dumps(value, sort_keys=True, separators=(",", ":"), allow_nan=False)


def init(db, add_col):
    add_col(db, "agents", "credential_version", "INTEGER NOT NULL DEFAULT 0")
    add_col(db, "jobs", "contract", "TEXT")
    add_col(db, "jobs", "artifact", "TEXT")
    add_col(db, "jobs", "evidence", "TEXT")
    add_col(db, "jobs", "acceptance_due", "REAL")
    db.executescript("""
        CREATE TABLE IF NOT EXISTS task_events(
            sequence INTEGER PRIMARY KEY AUTOINCREMENT, job_id TEXT NOT NULL,
            actor TEXT NOT NULL, kind TEXT NOT NULL, detail TEXT NOT NULL,
            created_at TEXT NOT NULL);
        CREATE INDEX IF NOT EXISTS task_events_job ON task_events(job_id, sequence);
        CREATE TABLE IF NOT EXISTS task_receipts(
            actor TEXT NOT NULL, key TEXT NOT NULL, fingerprint TEXT NOT NULL,
            response TEXT NOT NULL, PRIMARY KEY(actor, key));
        CREATE TABLE IF NOT EXISTS retired_names(name TEXT PRIMARY KEY);
    """)


def authorize(db, headers, actor):
    if not isinstance(actor, str) or not NAME.fullmatch(actor):
        raise Problem(400, "invalid_actor", "a registered agent name is required")
    row = db.execute("SELECT secret, credential_version FROM agents WHERE name=?",
                     (actor,)).fetchone()
    token = headers.get("Authorization", "")
    if not row or row[1] != 1 or not token.startswith("Bearer ") or not secrets.compare_digest(
            hashlib.sha256(token[7:].encode()).hexdigest(), row[0]):
        raise Problem(403, "invalid_credentials",
                      "valid Bearer credentials required; legacy credentials need operator recovery")


def event(db, hub, job_id, actor, kind, detail):
    db.execute("INSERT INTO task_events(job_id,actor,kind,detail,created_at) VALUES(?,?,?,?,?)",
               (job_id, actor, kind, encoded(detail), hub.now_iso()))


def notify(db, hub, box, actor, kind, job_id):
    db.execute("INSERT INTO messages(id,box,sender,mtype,body,created_at,expires) VALUES(?,?,?,?,?,?,?)",
               (str(uuid.uuid4()), box, actor, kind, encoded({"job_id": job_id}),
                hub.now_iso(), time.time() + hub.MSG_TTL_DEFAULT * 3600))


def expire(db, hub):
    for job_id, status in db.execute(
            "SELECT id,status FROM jobs WHERE contract IS NOT NULL AND "
            "((status IN ('open','claimed') AND expires<=?) OR "
            "(status='submitted' AND acceptance_due<=?))", (time.time(), time.time())).fetchall():
        target = "acceptance_expired" if status == "submitted" else "expired"
        db.execute("UPDATE jobs SET status=?,completed_at=? WHERE id=?",
                   (target, hub.now_iso(), job_id))
        event(db, hub, job_id, "system", target, {"previous_status": status})


def number(body, key, default, maximum):
    value = body.get(key, default)
    if not finite_number(value) or not 0 < value <= maximum:
        raise Problem(400, "invalid_contract", f"{key} must be finite, positive and <= {maximum}")
    return value


def finite_number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return False
    try:
        return math.isfinite(value)
    except OverflowError:
        return False


def strings(body, key, required=False):
    value = body.get(key, [])
    if not isinstance(value, list) or len(value) > 32 or (required and not value) or any(
            not isinstance(x, str) or not x.strip() or len(x) > 1000 for x in value):
        raise Problem(400, "invalid_contract", f"{key} must contain up to 32 nonempty strings (1000 chars each)")
    return value


def required_text(body, key, maximum):
    value = body.get(key)
    if not isinstance(value, str) or not value.strip() or len(value) > maximum:
        raise Problem(400, "invalid_field", f"{key} must be a nonempty string <= {maximum} chars")
    return value


def mutate(db, hub, path, body, actor):
    if path == "/v1/jobs":
        title = required_text(body, "title", 140)
        description = required_text(body, "description", 2000)
        capability = body.get("capability", "general")
        if not isinstance(capability, str) or not NAME.fullmatch(capability):
            raise Problem(400, "invalid_capability", "capability must be a stable name <= 64 chars")
        budget = body.get("budget", {})
        if not isinstance(budget, dict) or set(budget) - {"max_tokens", "max_cost", "currency"}:
            raise Problem(400, "invalid_budget", "budget accepts max_tokens, max_cost and currency")
        for key in ("max_tokens", "max_cost"):
            if key in budget:
                value = budget[key]
                if not finite_number(value) or value < 0:
                    raise Problem(400, "invalid_budget", f"{key} must be finite and nonnegative")
        if "max_tokens" in budget and not isinstance(budget["max_tokens"], int):
            raise Problem(400, "invalid_budget", "max_tokens must be an integer")
        if "currency" in budget and (not isinstance(budget["currency"], str) or not re.fullmatch(r"[A-Z]{3}", budget["currency"])):
            raise Problem(400, "invalid_budget", "currency must be a three-letter uppercase code")
        if "max_cost" in budget and "currency" not in budget:
            raise Problem(400, "invalid_budget", "max_cost requires currency")
        contract = {"version": 1, "acceptance_criteria": strings(body, "acceptance_criteria", True),
                    "constraints": strings(body, "constraints"), "budget": budget,
                    "acceptance_hours": number(body, "acceptance_hours", 72, 168)}
        ttl = number(body, "ttl_hours", 72, 168)
        payload = encoded(body.get("payload"))
        if len(payload.encode()) > 16384:
            raise Problem(400, "payload_too_large", "payload exceeds 16KB")
        if db.execute("SELECT COUNT(*) FROM jobs WHERE poster=? AND status IN ('open','claimed','submitted')", (actor,)).fetchone()[0] >= hub.JOB_MAX_OPEN_PER_POSTER:
            raise Problem(429, "too_many_jobs", "too many active tasks")
        job_id = str(uuid.uuid4())
        db.execute("INSERT INTO jobs(id,title,description,capability,payload,poster,status,created_at,expires,contract) VALUES(?,?,?,?,?,?,'open',?,?,?)",
                   (job_id, title, description, capability, payload, actor, hub.now_iso(),
                    time.time() + ttl * 3600, encoded(contract)))
        event(db, hub, job_id, actor, "open", {"contract": contract})
        notified = 0
        for name, caps in db.execute("SELECT name,capabilities FROM agents WHERE credential_version=1 ORDER BY name"):
            if name != actor and capability in json.loads(caps or "[]"):
                notify(db, hub, name, actor, "job_offer", job_id)
                notified += 1
                if notified >= 50:
                    break
        return {"job_id": job_id, "status": "open", "contract": contract}

    _, _, _, job_id, action = path.split("/")
    row = db.execute("SELECT poster,worker,status,contract,rated FROM jobs WHERE id=?", (job_id,)).fetchone()
    if not row:
        raise Problem(404, "unknown_job", "no job with this id")
    poster, worker, status, contract, rated = row
    if contract is None:
        raise Problem(409, "legacy_job", "legacy tasks are read-only; create a new contracted task")
    expected = poster if action in ("accept", "reject", "rate") else worker
    if action != "claim" and actor != expected:
        raise Problem(403, "wrong_actor", "only the assigned participant may perform this action")
    allowed = {"claim": ["open"], "complete": ["claimed"], "fail": ["claimed"],
               "accept": ["submitted"], "reject": ["submitted"], "rate": ["accepted", "rejected"]}
    if status not in allowed[action]:
        raise Problem(409, "wrong_status", f"cannot {action} a {status} task")
    detail = {}
    if action == "claim":
        if actor == poster:
            raise Problem(400, "self_claim", "posters cannot claim their own tasks")
        db.execute("UPDATE jobs SET worker=?,status='claimed',claimed_at=? WHERE id=?", (actor, hub.now_iso(), job_id))
        target = "claimed"
    elif action == "complete":
        if "result" not in body or body["result"] is None:
            raise Problem(400, "missing_result", "a non-null result is required")
        artifact = required_text(body, "artifact", 2000)
        evidence = strings(body, "evidence", True)
        result = encoded(body["result"])
        if len(result.encode()) > hub.JOB_RESULT_MAX:
            raise Problem(400, "result_too_large", "result exceeds 32KB")
        due = time.time() + json.loads(contract)["acceptance_hours"] * 3600
        detail = {"artifact": artifact, "evidence": evidence, "result_sha256": hashlib.sha256(result.encode()).hexdigest()}
        db.execute("UPDATE jobs SET status='submitted',result=?,artifact=?,evidence=?,acceptance_due=? WHERE id=?",
                   (result, artifact, encoded(evidence), due, job_id))
        target = "submitted"
    elif action in ("accept", "reject", "fail"):
        detail = {"reason": required_text(body, "reason", 2000)}
        target = {"accept": "accepted", "reject": "rejected", "fail": "failed"}[action]
        db.execute("UPDATE jobs SET status=?,completed_at=? WHERE id=?", (target, hub.now_iso(), job_id))
    else:
        stars = body.get("stars")
        if type(stars) is not int or not 1 <= stars <= 5:
            raise Problem(400, "invalid_stars", "stars must be an integer 1-5")
        if rated:
            raise Problem(409, "already_rated", "one rating per task")
        detail = {"stars": stars}
        db.execute("UPDATE jobs SET rated=1 WHERE id=?", (job_id,))
        target = status
    event(db, hub, job_id, actor, "rated" if action == "rate" else target, detail)
    notify(db, hub, worker if actor == poster else poster, actor, "job_" + action, job_id)
    return {"job_id": job_id, "status": target, **detail}


def read(db, hub, path, query):
    if path.startswith("/v1/reputation"):
        name = path[len("/v1/reputation/"):] if path != "/v1/reputation" else None
        params = (name,) if name else ()
        rows = db.execute("SELECT worker,capability,status,COUNT(*) FROM jobs WHERE contract IS NOT NULL AND worker IS NOT NULL" +
                          (" AND worker=?" if name else "") + " GROUP BY worker,capability,status ORDER BY worker,capability,status", params).fetchall()
        records = {}
        for worker, capability, status, count in rows:
            record = records.setdefault((worker, capability), {"name": worker, "capability": capability, "outcomes": {}})
            record["outcomes"][status] = count
        ratings = db.execute("SELECT j.worker,j.capability,e.detail FROM task_events e JOIN jobs j ON j.id=e.job_id WHERE e.kind='rated'" +
                             (" AND j.worker=?" if name else ""), params).fetchall()
        for worker, capability, detail in ratings:
            record = records[(worker, capability)]
            record.setdefault("ratings", []).append(json.loads(detail)["stars"])
        for record in records.values():
            stars = record.pop("ratings", [])
            record["ratings"] = len(stars)
            record["avg_rating"] = round(sum(stars) / len(stars), 2) if stars else None
        return {"records": list(records.values()), "basis": "requester decisions; not independent certification",
                "legacy_excluded": True}
    if path == "/v1/jobs":
        status = query.get("status", ["open"])[0]
        if status not in STATES + ["all"]:
            raise Problem(400, "invalid_status", "unknown task status")
        clauses, params = [], []
        if status != "all":
            clauses.append("status=?")
            params.append(status)
        if "capability" in query:
            clauses.append("capability=?")
            params.append(query["capability"][0])
        rows = db.execute("SELECT id,title,capability,poster,worker,status,contract IS NULL FROM jobs" +
                          (" WHERE " + " AND ".join(clauses) if clauses else "") + " ORDER BY created_at DESC,id LIMIT 100", params).fetchall()
        return {"jobs": [dict(zip(("job_id", "title", "capability", "poster", "worker", "status", "legacy"), row)) for row in rows], "count": len(rows)}
    job_id = path.rsplit("/", 1)[1]
    cursor = db.execute("SELECT * FROM jobs WHERE id=?", (job_id,))
    row = cursor.fetchone()
    if not row:
        raise Problem(404, "unknown_job", "no job with this id")
    result = dict(zip((col[0] for col in cursor.description), row))
    result["job_id"] = result.pop("id")
    for key in ("payload", "result", "contract", "evidence"):
        result[key] = json.loads(result[key]) if result[key] else None
    result["legacy"] = result["contract"] is None
    result["events"] = [{"actor": actor, "kind": kind, "detail": json.loads(detail), "created_at": at}
                        for actor, kind, detail, at in db.execute("SELECT actor,kind,detail,created_at FROM task_events WHERE job_id=? ORDER BY sequence", (job_id,))]
    return result


def dispatch(handler, hub, method, path, query):
    recognized = path == "/v1/jobs" or re.fullmatch(r"/v1/jobs/[0-9a-f-]{36}(?:/(claim|complete|fail|accept|reject|rate))?", path) or re.fullmatch(r"/v1/reputation(?:/[A-Za-z0-9_.-]{1,64})?", path)
    if not recognized:
        return False
    try:
        # Never hold the shared database lock while waiting for network input.
        body = handler.read_json() if method == "POST" else None
        with hub.DB_LOCK:
            with hub.DB:
                expire(hub.DB, hub)
            if method == "GET" and not path.endswith(tuple("/" + a for a in ("claim", "complete", "fail", "accept", "reject", "rate"))):
                response = read(hub.DB, hub, path, query)
            elif method == "POST" and (path == "/v1/jobs" or re.fullmatch(r"/v1/jobs/[0-9a-f-]{36}/(claim|complete|fail|accept|reject|rate)", path)):
                if not isinstance(body, dict):
                    raise Problem(400, "invalid_body", "JSON object required")
                actor = body.get("poster" if path == "/v1/jobs" or path.endswith(("/accept", "/reject", "/rate")) else "worker")
                authorize(hub.DB, handler.headers, actor)
                key = handler.headers.get("Idempotency-Key", "")
                if not re.fullmatch(r"[A-Za-z0-9_.:-]{1,128}", key):
                    raise Problem(400, "idempotency_key_required", "provide Idempotency-Key (1-128 safe ASCII characters)")
                fingerprint = hashlib.sha256((method + " " + path + "\n" + encoded(body)).encode()).hexdigest()
                with hub.DB:
                    receipt = hub.DB.execute("SELECT fingerprint,response FROM task_receipts WHERE actor=? AND key=?", (actor, key)).fetchone()
                    if receipt:
                        if receipt[0] != fingerprint:
                            raise Problem(409, "idempotency_conflict", "key already used with another path or body")
                        response = json.loads(receipt[1])
                    else:
                        response = mutate(hub.DB, hub, path, body, actor)
                        hub.DB.execute("INSERT INTO task_receipts VALUES(?,?,?,?)", (actor, key, fingerprint, encoded(response)))
            else:
                raise Problem(405, "method_not_allowed", "unsupported task operation")
        hub.notify_cond()
        handler.send_json(response)
    except Problem as exc:
        handler.error(exc.status, exc.code, exc.message)
    return True


def configure_discovery(openapi, manifest, card):
    """Publish the v3 REST contract consistently in both discovery surfaces."""
    text = {"type": "string", "minLength": 1}
    actor = {"type": "string", "pattern": r"^[A-Za-z0-9_.-]{1,64}$"}
    string_list = {"type": "array", "maxItems": 32, "items": {**text, "maxLength": 1000}}
    positive_hours = {"type": "number", "exclusiveMinimum": 0, "maximum": 168, "default": 72}
    bodies = {
        "": (["poster", "title", "description", "acceptance_criteria"], {
            "poster": actor, "title": {**text, "maxLength": 140},
            "description": {**text, "maxLength": 2000}, "capability": {**actor, "default": "general"},
            "payload": {}, "acceptance_criteria": {**string_list, "minItems": 1},
            "constraints": string_list, "budget": {"type": "object", "additionalProperties": False,
                "description": "Declared remote budget; not enforced by the hub. max_cost requires currency.",
                "dependentRequired": {"max_cost": ["currency"]},
                "properties": {"max_tokens": {"type": "integer", "minimum": 0},
                               "max_cost": {"type": "number", "minimum": 0},
                               "currency": {"type": "string", "pattern": "^[A-Z]{3}$"}}},
            "ttl_hours": positive_hours, "acceptance_hours": positive_hours}),
        "claim": (["worker"], {"worker": actor}),
        "complete": (["worker", "result", "artifact", "evidence"], {
            "worker": actor, "result": {"not": {"type": "null"}},
            "artifact": {**text, "maxLength": 2000}, "evidence": {**string_list, "minItems": 1}}),
        "fail": (["worker", "reason"], {"worker": actor, "reason": {**text, "maxLength": 2000}}),
        "accept": (["poster", "reason"], {"poster": actor, "reason": {**text, "maxLength": 2000}}),
        "reject": (["poster", "reason"], {"poster": actor, "reason": {**text, "maxLength": 2000}}),
        "rate": (["poster", "stars"], {"poster": actor, "stars": {"type": "integer", "minimum": 1, "maximum": 5}}),
    }
    summaries = {"": "Post an immutable task contract", "claim": "Claim an open task",
                 "complete": "Submit result and evidence for requester acceptance",
                 "fail": "Record execution failure", "accept": "Requester accepts submitted result",
                 "reject": "Requester rejects submitted result", "rate": "Rate accepted or rejected task once"}
    security = [{"AgentBearer": []}]
    openapi.setdefault("components", {}).setdefault("securitySchemes", {})["AgentBearer"] = {
        "type": "http", "scheme": "bearer", "description": "Secret returned only on initial registration"}
    manifest["endpoints"] = [e for e in manifest["endpoints"] if not (e["path"].startswith("/v1/jobs") and e["method"] == "POST")]
    for action, (required, properties) in bodies.items():
        path = "/v1/jobs" + ("/{id}/" + action if action else "")
        parameters = [{"name": "Idempotency-Key", "in": "header", "required": True,
                       "schema": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,128}$"}}]
        if action:
            parameters.append({"name": "id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}})
        schema = {"type": "object", "required": required, "properties": properties}
        openapi["paths"].setdefault(path, {})["post"] = {
            "summary": summaries[action], "security": security, "parameters": parameters,
            "requestBody": {"required": True, "content": {"application/json": {"schema": schema}}},
            "responses": {str(code): {"description": desc} for code, desc in
                          ((200, "Committed result, or original replay"), (400, "Invalid request"),
                           (403, "Invalid credentials or participant"), (404, "Task missing"),
                           (409, "State or idempotency conflict"), (429, "Rate or active-task limit"))}}
        manifest["endpoints"].append({"method": "POST", "path": path, "desc": summaries[action],
                                      "required_headers": ["Authorization: Bearer <secret>", "Idempotency-Key"],
                                      "body_schema": schema})
    openapi["paths"]["/v1/jobs"]["get"]["parameters"][0]["schema"]["enum"] = STATES + ["all"]
    for path, method in (("/v1/presence", "post"), ("/v1/registry/{name}", "delete")):
        openapi["paths"][path][method]["security"] = security
        openapi["paths"][path][method]["responses"]["403"] = {"description": "Invalid credentials; legacy agents require recovery"}
    openapi["paths"]["/v1/registry/{name}"]["delete"].pop("requestBody", None)
    registration = openapi["paths"]["/v1/registry"]["post"]
    registration["security"] = [{}, {"AgentBearer": []}]
    registration["description"] = "New names need no account. Existing names require Bearer credentials. Secret returned once; legacy names require operator recovery."
    for path in ("/v1/reputation", "/v1/reputation/{name}"):
        openapi["paths"][path]["get"]["summary"] = "Per-capability requester-reported outcomes and ratings; legacy excluded"
    for e in manifest["endpoints"]:
        if e["path"] == "/v1/jobs" and e["method"] == "GET":
            e["params"]["status"] = "|".join(STATES + ["all"])
        if e["path"].startswith("/v1/reputation"):
            e["desc"] = "Per-capability task outcomes and ratings; legacy excluded; requester decisions, not certification"
        if e["path"] in ("/v1/presence", "/v1/registry/{name}") and e["method"] != "GET":
            e["required_headers"] = ["Authorization: Bearer <secret>"]
            if e["method"] == "DELETE":
                e.pop("body", None)
    for skill in card["skills"]:
        if skill["id"] == "registry":
            skill["examples"] = ["GET /v1/registry?capability=fetch", "Presence requires Authorization: Bearer <registration-secret>"]
        if skill["id"] == "jobs":
            skill["description"] = "Authenticated task contracts, delivery, requester acceptance and per-capability outcomes. See /openapi.json."
            skill["examples"] = ["GET /v1/jobs?status=open", "Read /openapi.json for authenticated task writes"]
