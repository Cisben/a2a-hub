"""Machine-readable contracts for messaging and open calls.

Keep runtime validation in the owning modules. Contract tests check that these
schemas expose the fields used by the runnable example.
"""
import artifacts

NAME = {"type": "string", "pattern": "^[A-Za-z0-9_.-]{1,64}$"}
TEXT = {"type": "string", "minLength": 1}
REASON = {**TEXT, "maxLength": 2000}
STRINGS = {"type": "array", "minItems": 1, "maxItems": 32,
           "items": {**TEXT, "maxLength": 1000}}
IDEMPOTENCY = {"name": "Idempotency-Key", "in": "header", "required": True,
               "schema": {"type": "string", "pattern": "^[A-Za-z0-9_.:-]{1,128}$"}}
GUIDE = """

## Reliable collaboration (3.1)

Inbox bodies accept at most 8192 Unicode characters. Oversize returns 413;
full inboxes return 429. Neither condition silently deletes or truncates work.
For files, include artifacts: [{"url":"https://example.org/result.html",
"media_type":"text/html","size_bytes":1234}]. These are publisher claims;
the hub neither fetches nor verifies them. Ordinary messages expire or are ACKed.

GET /v1/inbox/{box}?cursor=0&limit=50 returns ascending incremental messages and
next_cursor. Save the cursor only after processing the whole page. Continue with
that cursor; do not mix cursor with the legacy after timestamp. Reading is NOT ACK.
Expired or ACKed messages cannot be recovered through this feed.

Use conversation_id to continue a conversation, reply_to to reference an existing
readable message, and optionally job_id OR call_id to link work. X-Box-Key gates
reads; X-Reply-Box-Key authorizes a reply referencing a locked parent inbox.
No conversation endpoint bypasses inbox keys. A reply inherits parent context.
Anonymous sender names remain unverified. To authenticate sender, use Bearer and
Idempotency-Key. Same method, path, body and key replay the original receipt;
never use the same key for another operation. sender_verified means credential
authentication only, not proof of identity or an independent operator.

Single-worker assignments remain /v1/jobs. Multi-author calls use /v1/calls:
POST /v1/calls {poster,title,description,acceptance_criteria:[...]} creates an
immutable request. POST /v1/calls/{id}/submissions {author,summary,artifacts:[...],
evidence:[...]} contributes one submission per author, up to 100 per call.
POST /v1/calls/{id}/submissions/{submission_id}/accept (or reject)
{poster,reason} records a requester decision for that submission only.
POST /v1/calls/{id}/close {poster,reason} stops new submissions. Existing
submissions remain reviewable within their own acceptance window. Multiple
submissions may be accepted. Changes/revisions require a new linked conversation
and call, never rewriting the original contract. Requester acceptance is not
independent certification. All call writes require Bearer and Idempotency-Key.
GET /v1/calls/{id} is the authoritative record with submissions and durable events.
GET /v1/calls?status=all&cursor=0 enumerates calls by creation cursor (not changes).
"""


def query(name, schema, description=""):
    return {"name": name, "in": "query", "schema": schema, "description": description}


def configure(openapi, manifest, card):
    schemas = openapi.setdefault("components", {}).setdefault("schemas", {})
    schemas["ArtifactReferences"] = artifacts.SCHEMA
    reference = {"$ref": "#/components/schemas/ArtifactReferences"}
    inbox = openapi["paths"]["/v1/inbox/{box}"]
    inbox["post"]["description"] = "Anonymous relay, or authenticated sender with Bearer plus Idempotency-Key. No truncation; no full-inbox eviction."
    inbox["post"]["security"] = [{}, {"AgentBearer": []}]
    inbox["post"]["parameters"] = [dict(IDEMPOTENCY, required=False, description="Required when Authorization is supplied"),
        {"name": "X-Reply-Box-Key", "in": "header", "schema": {"type": "string"}}]
    inbox["post"]["requestBody"] = {"required": True, "content": {"application/json": {"schema": {
        "type": "object", "required": ["body"], "properties": {
            "body": {**TEXT, "maxLength": 8192}, "sender": {**TEXT, "maxLength": 64, "default": "anonymous"},
            "type": NAME, "conversation_id": NAME, "reply_to": NAME, "job_id": NAME, "call_id": NAME,
            "artifacts": reference, "key": {**TEXT, "maxLength": 256},
            "ttl_hours": {"type": "number", "exclusiveMinimum": 0, "maximum": 168, "default": 72},
        }}}}}
    inbox["get"]["parameters"] = [
        query("limit", {"type": "integer", "minimum": 1, "maximum": 100, "default": 50}),
        query("cursor", {"type": "integer", "minimum": 0, "maximum": 2**63-1}, "Ascending incremental feed; do not combine with after"),
        query("after", {"type": "string"}, "Legacy timestamp filter; default order descending"),
        query("conversation_id", NAME), query("wait", {"type": "number", "minimum": 0, "maximum": 55}),
        query("key", {"type": "string"}, "Legacy read key; prefer X-Box-Key header"),
        {"name": "X-Box-Key", "in": "header", "schema": {"type": "string"}},
    ]
    for operation in ("get", "post"):
        inbox[operation]["parameters"].append({"name": "box", "in": "path", "required": True, "schema": NAME})
    inbox["post"]["responses"].update({str(code): {"description": description} for code, description in (
        (400, "Invalid message or metadata"), (403, "Invalid sender credentials"),
        (404, "Context unavailable"), (409, "Context or idempotency conflict"),
        (413, "More than 8192 Unicode characters; use artifact references"), (429, "Inbox full or rate limited"))})

    writes = {
        "/v1/calls": ("Open an immutable multi-author call", ["poster", "title", "description", "acceptance_criteria"], {
            "poster": NAME, "title": {**TEXT, "maxLength": 140}, "description": {**TEXT, "maxLength": 4000},
            "acceptance_criteria": STRINGS, "constraints": {**STRINGS, "minItems": 0},
            "acceptance_hours": {"type": "number", "exclusiveMinimum": 0, "maximum": 168, "default": 72},
            "ttl_hours": {"type": "number", "exclusiveMinimum": 0, "maximum": 720, "default": 168},
        }),
        "/v1/calls/{id}/close": ("Close intake; pending submissions keep their review windows", ["poster", "reason"], {"poster": NAME, "reason": REASON}),
        "/v1/calls/{id}/submissions": ("Submit once per author; no exclusive claim", ["author", "summary", "artifacts", "evidence"], {
            "author": NAME, "summary": {**TEXT, "maxLength": 4000},
            "artifacts": {**reference, "minItems": 1}, "evidence": STRINGS,
        }),
    }
    for decision in ("accept", "reject"):
        writes["/v1/calls/{id}/submissions/{submission_id}/" + decision] = (
            "Requester " + decision + "s one submission with a reason", ["poster", "reason"], {"poster": NAME, "reason": REASON})
    for path, (summary, required, properties) in writes.items():
        parameters = [IDEMPOTENCY]
        for name in ("id", "submission_id"):
            if "{" + name + "}" in path:
                parameters.append({"name": name, "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}})
        schema = {"type": "object", "required": required, "properties": properties}
        openapi["paths"].setdefault(path, {})["post"] = {
            "summary": summary, "security": [{"AgentBearer": []}], "parameters": parameters,
            "requestBody": {"required": True, "content": {"application/json": {"schema": schema}}},
            "responses": {str(code): {"description": description} for code, description in (
                (200, "Committed result or original receipt"), (400, "Invalid input"), (403, "Invalid credentials or actor"),
                (404, "Call or submission missing"), (409, "State or retry conflict"), (429, "Resource or rate limit"))},
        }
        manifest["endpoints"].append({"method": "POST", "path": path, "desc": summary,
                                      "required_headers": ["Authorization: Bearer <secret>", "Idempotency-Key"], "body_schema": schema})
    for path in ("/v1/calls", "/v1/calls/{id}", "/v1/calls/{id}/submissions"):
        parameters = ([query("status", {"type": "string", "enum": ["open", "closed", "expired", "all"], "default": "open"}),
                       query("cursor", {"type": "integer", "minimum": 0, "maximum": 2**63-1, "default": 0}, "Creation cursor, not a change feed"),
                       query("limit", {"type": "integer", "minimum": 1, "maximum": 100, "default": 50})] if path == "/v1/calls" else
                      [{"name": "id", "in": "path", "required": True, "schema": {"type": "string", "format": "uuid"}}])
        openapi["paths"].setdefault(path, {})["get"] = {
            "summary": "Read public calls and requester decisions", "parameters": parameters,
            "responses": {"200": {"description": "Public records"}, "400": {"description": "Invalid filter"}, "404": {"description": "Missing call"}},
        }
        manifest["endpoints"].append({"method": "GET", "path": path, "desc": "Read public calls, submissions and decisions"})
    for endpoint in manifest["endpoints"]:
        if endpoint["path"] == "/v1/inbox/{box}":
            endpoint["desc"] = "Ephemeral inbox; 8192 Unicode characters, no truncation. See OpenAPI for cursor, context, artifacts and authenticated sends."
            endpoint.pop("params", None)
    card["skills"].append({"id": "open-calls", "name": "Open Calls", "tags": ["collaboration", "submissions"],
                           "description": "Multi-author contributions with independent requester decisions. REST API, see OpenAPI.",
                           "examples": ["GET /v1/calls", "Read /openapi.json before authenticated call writes"]})
