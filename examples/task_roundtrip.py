"""Local-only two-agent fixture. Does not represent independent adoption.

Start the hub with a temporary A2A_DB, then run this script. No external service
is contacted. For independent operators, split the requester/worker calls and
store each initial registration secret privately.
"""
import json
import urllib.request
import uuid


def run(base="http://127.0.0.1:8787"):
    def api(path, body=None, token=None, key=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if token:
            headers["Authorization"] = "Bearer " + token
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(base + path, data=json.dumps(body).encode() if body is not None else None, headers=headers)
        with urllib.request.urlopen(request, timeout=10) as response:
            return json.loads(response.read())

    suffix = uuid.uuid4().hex[:12]
    poster, worker = "fixture-poster-" + suffix, "fixture-worker-" + suffix
    def register(name):
        return api("/v1/registry", {"name": name, "endpoint": "https://example.invalid/fixture",
                                   "capabilities": ["fixture-extract"]})["secret"]
    poster_token, worker_token = register(poster), register(worker)
    job = api("/v1/jobs", {"poster": poster, "title": "Extract fixture amount", "description": "Return amount as an integer",
                          "capability": "fixture-extract", "payload": {"text": "Amount: 42"},
                          "acceptance_criteria": ["result equals {amount: 42}"],
                          "constraints": ["Use only the provided fixture"], "budget": {"max_tokens": 0}},
              poster_token, "create")["job_id"]
    path = "/v1/jobs/" + job
    api(path + "/claim", {"worker": worker}, worker_token, "claim")
    api(path + "/complete", {"worker": worker, "result": {"amount": 42}, "artifact": "inline:result",
                             "evidence": ["Parsed Amount: 42 from the supplied fixture"]}, worker_token, "submit")
    delivered = api(path)
    if delivered["result"] != {"amount": 42}:
        api(path + "/reject", {"poster": poster, "reason": "Unexpected amount"}, poster_token, "decision")
        raise RuntimeError("Fixture verification failed")
    api(path + "/accept", {"poster": poster, "reason": "Exact fixture comparison passed"}, poster_token, "decision")
    api(path + "/rate", {"poster": poster, "stars": 5}, poster_token, "rating")
    return api(path)


if __name__ == "__main__":
    result = run()
    print(json.dumps({"job_id": result["job_id"], "status": result["status"], "fixture": True}))
