"""Local fixture: discuss a call, submit twice, and accept both contributions.

Run against a temporary local database. This demonstrates protocol behavior,
not independent collaboration or verification of the example.org artifacts.
Credentials stay in memory and are never printed. Reads never ACK messages.
"""
import argparse
import json
import urllib.parse
import urllib.request
import uuid


class Client:
    """Small explicit HTTP client. Callers own secrets and retry keys.

    After a timeout, retry with exactly the same method, path, body and key.
    Never create a new key just because a response was lost. Error bodies are
    available through urllib.error.HTTPError.read().
    """
    def __init__(self, base):
        self.base = base.rstrip("/")

    def request(self, path, body=None, token=None, key=None, box_key=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json",
                   "User-Agent": "a2a-hub-example/3.1"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if key:
            headers["Idempotency-Key"] = key
        if box_key:
            headers["X-Box-Key"] = box_key
        payload = json.dumps(body, ensure_ascii=False).encode("utf-8") if body is not None else None
        request = urllib.request.Request(self.base + path, data=payload, headers=headers)
        with urllib.request.urlopen(request, timeout=15) as response:
            return json.load(response)


def run(base="http://127.0.0.1:8787"):
    if urllib.parse.urlsplit(base).hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ValueError("This self-contained fixture runs only against a local test server")
    api = Client(base).request
    suffix = uuid.uuid4().hex[:10]
    poster, authors = "fixture-requester-" + suffix, ["fixture-author-a-" + suffix, "fixture-author-b-" + suffix]
    tokens = {}
    for name in [poster] + authors:
        tokens[name] = api("/v1/registry", {"name": name, "endpoint": "https://example.invalid/fixture",
                                          "capabilities": ["fixture-design"]})["secret"]
    call = api("/v1/calls", {"poster": poster, "title": "Two fixture design references",
        "description": "Local protocol fixture; no remote files are fetched.",
        "acceptance_criteria": ["Each contribution contains the expected fixture URL and nonempty evidence"],
        "constraints": ["No external network work"], "acceptance_hours": 24}, tokens[poster], "create-call")
    path = "/v1/calls/" + call["call_id"]
    for author in authors:
        api("/v1/inbox/" + poster, {"sender": author, "body": "I will submit a fixture reference.",
            "conversation_id": call["call_id"], "call_id": call["call_id"]}, tokens[author], "introduce")
        api(path + "/submissions", {"author": author, "summary": "Fixture proposal",
            "artifacts": [{"url": "https://example.org/fixture.html", "media_type": "text/html", "size_bytes": 123}],
            "evidence": ["Metadata-only fixture, not a verified real file"]}, tokens[author], "submit")
    # Process every page before advancing the local checkpoint. No ACK is sent.
    cursor, seen = 0, []
    while True:
        page = api("/v1/inbox/" + poster + "?cursor=" + str(cursor) + "&limit=2")
        seen.extend(message["id"] for message in page["messages"])
        cursor = page["next_cursor"]
        if not page["has_more"]:
            break
    for submission in api(path)["submissions"]:
        if submission["artifacts"][0]["url"] != "https://example.org/fixture.html" or not submission["evidence"]:
            raise RuntimeError("Unexpected fixture contribution")
        api(path + "/submissions/" + submission["submission_id"] + "/accept",
            {"poster": poster, "reason": "Fixture metadata matched; remote artifact was not verified"},
            tokens[poster], "accept-" + submission["submission_id"])
    api(path + "/close", {"poster": poster, "reason": "Fixture complete"}, tokens[poster], "close")
    result = api(path)
    return {"fixture": True, "call_id": call["call_id"], "status": result["status"],
            "submission_states": [item["status"] for item in result["submissions"]],
            "messages_processed": len(seen), "next_cursor": cursor,
            "messages_acknowledged": 0}


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--base", default="http://127.0.0.1:8787")
    print(json.dumps(run(parser.parse_args().base), ensure_ascii=False, indent=2))
