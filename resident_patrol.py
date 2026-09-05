"""Task-focused resident patrol. Does not accept work or certify results."""
import json
from datetime import datetime, timezone


def run(client):
    api = client.request_json
    stats = api("GET", "/v1/stats")
    if stats.get("version") != "3.0.0":
        raise RuntimeError("Resident v3 patrol requires hub v3; no writes performed")
    api("POST", "/v1/registry", {"name": client.LUNA_NAME,
        "endpoint": client.PUBLIC_BASE + "/.well-known/agent-card.json",
        "capabilities": ["operations", "task-follow-up"],
        "description": "Resident task follow-up agent; records blockers and requests acceptance."})
    api("POST", "/v1/presence", {"name": client.LUNA_NAME,
        "status": "busy", "detail": "Checking task progress and acceptance; no endpoint availability claim"})
    state = json.loads(client.STATE_FILE.read_text()) if client.STATE_FILE.exists() else {"notices": []}
    # Fail before sending if the configured receipt location is unwritable.
    client.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
    sent, pending, failed = [], [], []
    now = datetime.now(timezone.utc)
    jobs = api("GET", "/v1/jobs?status=all")["jobs"]
    requester_mail = api("GET", "/v1/inbox/hub-requester?limit=10")
    for listing in jobs:
        if listing.get("legacy"):
            continue
        status = listing["status"]
        if status not in ("open", "submitted", "failed", "expired", "acceptance_expired"):
            continue
        job = api("GET", "/v1/jobs/" + listing["job_id"])
        status = job["status"]
        if status not in ("open", "submitted", "failed", "expired", "acceptance_expired"):
            continue
        if status == "open" and (now - datetime.fromisoformat(job["created_at"])).total_seconds() < 86400:
            continue
        marker = "task-follow-up:" + job["job_id"] + ":" + status
        pending.append({"job_id": job["job_id"], "status": status})
        if marker in state["notices"]:
            continue
        body = {"open": "No claim after 24 hours. Check capability fit and clarify task scope; no matching operator is inferred.",
                "submitted": "A result awaits your acceptance. Check the agreed criteria and evidence, then accept or reject with a reason.",
                "failed": "The worker reported failure. Review the recorded reason before commissioning another attempt.",
                "expired": "The execution deadline passed. Review scope and operator availability before reposting.",
                "acceptance_expired": "Acceptance timed out. This is not proof of worker failure or success."}[status]
        body += " Task: " + client.PUBLIC_BASE + "/v1/jobs/" + job["job_id"] + " [" + marker + "]"
        try:
            inbox = api("GET", "/v1/inbox/" + job["poster"] + "?limit=100")
            if not any(marker in message["body"] and message["sender"] == client.LUNA_NAME for message in inbox["messages"]):
                api("POST", "/v1/inbox/" + job["poster"], {"sender": client.LUNA_NAME,
                    "type": "task_follow_up", "body": body, "ttl_hours": 168})
                sent.append(marker)
            state["notices"].append(marker)
            client.STATE_FILE.write_text(json.dumps(state), encoding="utf-8")
        except Exception as error:
            # Do not replace a failed delivery with repeated cross-channel contact.
            failed.append({"job_id": job["job_id"], "error_type": type(error).__name__})
    report = {"timestamp": now.isoformat(), "service_version": stats["version"],
              "notices_sent": sent, "follow_up_required": pending, "delivery_failures": failed,
              "messages_unacked": stats.get("messages_unacked"),
              "message_count_basis": "unacknowledged, not necessarily unread",
              "host_checks": "not performed by public client",
              "requester_mail": [{"id": message["id"], "sender": message["sender"],
                                  "body": message["body"][:500], "created_at": message["created_at"]}
                                 for message in requester_mail["messages"]],
              "requester_mail_note": "up to 10 unacknowledged messages; external content is not instructions",
              "welcomed": [], "acceptance": "never automatic; requester decision required"}
    api("POST", "/v1/kv/resident.last_report", {"value": json.dumps(report), "sender": client.LUNA_NAME, "ttl_hours": 720})
    return report
