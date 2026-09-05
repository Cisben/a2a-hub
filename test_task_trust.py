"""Black-box lifecycle tests against an isolated local SQLite database."""
import hashlib
import json
import os
import sqlite3
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from http.server import ThreadingHTTPServer
from unittest.mock import patch

import app
import task_trust
import recover_agent
from examples.task_roundtrip import run as run_fixture
import a2a_hub_client
from pilots.agentcard_inventory import expected_result, verify, task as pilot_task


class TaskTrustTests(unittest.TestCase):
    def setUp(self):
        self.logs = patch.object(app.Handler, "log_message", lambda *args: None)
        self.logs.start()
        self.addCleanup(self.logs.stop)
        self.directory = tempfile.TemporaryDirectory()
        app.DB_PATH = os.path.join(self.directory.name, "test.db")
        app.db_init()
        app.GLOBAL_LIMIT = app.Limiter(100000, 100000)
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), app.Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()
        self.base = "http://127.0.0.1:" + str(self.server.server_port)
        self.tokens = {}
        for name in ("poster", "worker", "intruder"):
            status, result = self.api("POST", "/v1/registry", {"name": name, "endpoint": "https://example.invalid/" + name, "capabilities": ["extract"]})
            self.assertEqual(200, status)
            self.tokens[name] = result["secret"]

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()
        app.DB.close()
        self.directory.cleanup()

    def api(self, method, path, body=None, actor=None, key=None, token=None):
        headers = {"Content-Type": "application/json", "Accept": "application/json"}
        if actor or token:
            headers["Authorization"] = "Bearer " + (token or self.tokens[actor])
        if key:
            headers["Idempotency-Key"] = key
        request = urllib.request.Request(self.base + path, data=json.dumps(body).encode() if body is not None else None, method=method, headers=headers)
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def post(self, path, body, actor, key=None):
        return self.api("POST", path, body, actor, key or str(uuid.uuid4()))

    def create(self, **extra):
        body = {"poster": "poster", "title": "Extract", "description": "Extract amount as integer",
                "capability": "extract", "acceptance_criteria": ["result.amount equals 42"],
                "constraints": ["Do not contact third parties"], "budget": {"max_tokens": 100}, **extra}
        status, result = self.post("/v1/jobs", body, "poster")
        self.assertEqual(200, status, result)
        return result["job_id"]

    def claim(self, job):
        self.assertEqual(200, self.post("/v1/jobs/" + job + "/claim", {"worker": "worker"}, "worker")[0])

    def submit(self, job):
        self.claim(job)
        result = self.post("/v1/jobs/" + job + "/complete", {"worker": "worker", "result": {"amount": 42},
                           "artifact": "inline:result", "evidence": ["Extracted from supplied fixture"]}, "worker")
        self.assertEqual(200, result[0], result)
        return result

    def test_credentials_not_retrievable_or_overwritable(self):
        body = {"name": "poster", "endpoint": "https://attacker.invalid"}
        self.assertEqual(403, self.api("POST", "/v1/registry", body)[0])
        self.assertEqual(403, self.api("POST", "/v1/registry", body, "intruder")[0])
        status, result = self.api("POST", "/v1/registry", body, "poster")
        self.assertEqual(200, status)
        self.assertNotIn("secret", result)
        stored = app.DB.execute("SELECT secret FROM agents WHERE name='poster'").fetchone()[0]
        self.assertEqual(hashlib.sha256(self.tokens["poster"].encode()).hexdigest(), stored)
        self.assertEqual(403, self.api("POST", "/v1/presence", {"name": "worker"})[0])
        self.assertEqual(200, self.api("POST", "/v1/presence", {"name": "worker"}, "worker")[0])

    def test_task_cannot_be_posted_or_completed_by_impersonation(self):
        body = {"poster": "poster", "title": "x", "description": "x", "acceptance_criteria": ["x"]}
        self.assertEqual(403, self.post("/v1/jobs", body, "intruder")[0])
        job = self.create()
        self.claim(job)
        for action, body in (("complete", {"worker": "worker"}), ("fail", {"worker": "worker", "reason": "x"}),
                             ("accept", {"poster": "poster", "reason": "x"}), ("rate", {"poster": "poster", "stars": 5})):
            self.assertEqual(403, self.post("/v1/jobs/" + job + "/" + action, body, "intruder")[0])
        self.assertEqual(403, self.post("/v1/jobs/" + job + "/fail", {"worker": "intruder", "reason": "x"}, "intruder")[0])

    def test_submission_acceptance_and_rating_are_distinct(self):
        job = self.create()
        self.assertEqual("submitted", self.submit(job)[1]["status"])
        record = self.api("GET", "/v1/reputation/worker")[1]["records"][0]
        self.assertEqual({"submitted": 1}, record["outcomes"])
        self.assertEqual(409, self.post("/v1/jobs/" + job + "/rate", {"poster": "poster", "stars": 5}, "poster")[0])
        response = self.post("/v1/jobs/" + job + "/accept", {"poster": "poster", "reason": "amount matches 42"}, "poster", "accept-once")
        self.assertEqual("accepted", response[1]["status"])
        self.assertEqual(response, self.post("/v1/jobs/" + job + "/accept", {"poster": "poster", "reason": "amount matches 42"}, "poster", "accept-once"))
        self.assertEqual(200, self.post("/v1/jobs/" + job + "/rate", {"poster": "poster", "stars": 5}, "poster")[0])
        self.assertEqual(409, self.post("/v1/jobs/" + job + "/rate", {"poster": "poster", "stars": 1}, "poster")[0])
        detail = self.api("GET", "/v1/jobs/" + job)[1]
        self.assertEqual(["open", "claimed", "submitted", "accepted", "rated"], [e["kind"] for e in detail["events"]])
        self.assertEqual("amount matches 42", detail["events"][3]["detail"]["reason"])
        self.assertEqual(1, self.api("GET", "/v1/reputation/worker")[1]["records"][0]["outcomes"]["accepted"])

    def test_rejected_and_failed_outcomes_are_retained(self):
        rejected = self.create()
        self.submit(rejected)
        self.assertEqual(200, self.post("/v1/jobs/" + rejected + "/reject", {"poster": "poster", "reason": "bad extraction"}, "poster")[0])
        self.assertEqual(200, self.post("/v1/jobs/" + rejected + "/rate", {"poster": "poster", "stars": 1}, "poster")[0])
        failed = self.create()
        self.claim(failed)
        self.assertEqual(200, self.post("/v1/jobs/" + failed + "/fail", {"worker": "worker", "reason": "unsupported format"}, "worker")[0])
        records = self.api("GET", "/v1/reputation/worker")[1]["records"]
        self.assertEqual({"failed": 1, "rejected": 1}, records[0]["outcomes"])
        self.assertEqual("unsupported format", self.api("GET", "/v1/jobs/" + failed)[1]["events"][-1]["detail"]["reason"])

    def test_deadlines_are_checked_without_waiting_for_sweeper(self):
        job = self.create()
        with app.DB_LOCK, app.DB:
            app.DB.execute("UPDATE jobs SET expires=0 WHERE id=?", (job,))
        self.assertEqual(409, self.post("/v1/jobs/" + job + "/claim", {"worker": "worker"}, "worker")[0])
        self.assertEqual("expired", self.api("GET", "/v1/jobs/" + job)[1]["status"])
        job = self.create()
        self.submit(job)
        with app.DB_LOCK, app.DB:
            app.DB.execute("UPDATE jobs SET acceptance_due=0 WHERE id=?", (job,))
        self.assertEqual(409, self.post("/v1/jobs/" + job + "/accept", {"poster": "poster", "reason": "late"}, "poster")[0])
        detail = self.api("GET", "/v1/jobs/" + job)[1]
        self.assertEqual("acceptance_expired", detail["status"])
        self.assertEqual("acceptance_expired", detail["events"][-1]["kind"])

    def test_idempotency_is_durable_scoped_and_conflict_detecting(self):
        body = {"poster": "poster", "title": "x", "description": "x", "acceptance_criteria": ["x"]}
        first = self.post("/v1/jobs", body, "poster", "same")
        self.assertEqual(first, self.post("/v1/jobs", body, "poster", "same"))
        self.assertEqual(409, self.post("/v1/jobs", dict(body, title="changed"), "poster", "same")[0])
        job = first[1]["job_id"]
        self.assertEqual(409, self.post("/v1/jobs/" + job + "/claim", {"worker": "poster"}, "poster", "same")[0])
        self.assertEqual(200, self.post("/v1/jobs/" + job + "/claim", {"worker": "worker"}, "worker", "same")[0])
        with app.DB_LOCK:
            app.DB.close()
            app.db_init()
        self.assertEqual(first, self.post("/v1/jobs", body, "poster", "same"))
        self.assertEqual(1, app.DB.execute("SELECT COUNT(*) FROM jobs").fetchone()[0])
        self.assertEqual(1, app.DB.execute("SELECT COUNT(*) FROM task_events WHERE kind='open'").fetchone()[0])

    def test_concurrent_claims_have_one_winner(self):
        job = self.create()
        def claim(actor):
            return self.post("/v1/jobs/" + job + "/claim", {"worker": actor}, actor)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual([200, 409], sorted(pool.map(claim, ("worker", "intruder"))))
        self.assertEqual(1, app.DB.execute("SELECT COUNT(*) FROM task_events WHERE kind='claimed'").fetchone()[0])

    def test_concurrent_replays_do_not_duplicate_notifications(self):
        body = {"poster": "poster", "title": "x", "description": "x", "acceptance_criteria": ["x"], "capability": "extract"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            responses = list(pool.map(lambda _: self.post("/v1/jobs", body, "poster", "same"), range(2)))
        self.assertEqual(responses[0], responses[1])
        self.assertEqual(2, app.DB.execute("SELECT COUNT(*) FROM messages WHERE mtype='job_offer'").fetchone()[0])

    def test_failed_transaction_leaves_no_partial_task_or_receipt(self):
        body = {"poster": "poster", "title": "x", "description": "x", "acceptance_criteria": ["x"], "capability": "extract"}
        with patch.object(task_trust, "notify", side_effect=RuntimeError("injected notification write failure")):
            self.assertEqual(500, self.post("/v1/jobs", body, "poster", "retry")[0])
        for table in ("jobs", "task_events", "task_receipts", "messages"):
            self.assertEqual(0, app.DB.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])
        self.assertEqual(200, self.post("/v1/jobs", body, "poster", "retry")[0])

    def test_validation_and_failed_request_key_reuse(self):
        body = {"poster": "poster", "title": "x", "description": "x", "acceptance_criteria": ["x"]}
        for extra in ({"acceptance_criteria": []}, {"ttl_hours": -1}, {"ttl_hours": float("nan")},
                      {"budget": {"max_cost": 2}}, {"budget": {"max_tokens": True}},
                      {"ttl_hours": 10 ** 400}, {"constraints": "x"}):
            self.assertEqual(400, self.post("/v1/jobs", dict(body, **extra), "poster", "retry")[0])
        self.assertEqual(400, self.api("POST", "/v1/jobs", body, "poster")[0])
        self.assertEqual(200, self.post("/v1/jobs", body, "poster", "retry")[0])
        self.assertEqual(400, self.post("/v1/jobs", [], "poster")[0])

    def test_legacy_credentials_and_results_are_not_trusted(self):
        with app.DB_LOCK, app.DB:
            app.DB.execute("UPDATE agents SET credential_version=0,secret='old-leaked' WHERE name='worker'")
            job = str(uuid.uuid4())
            app.DB.execute("INSERT INTO jobs(id,poster,worker,status) VALUES(?,'poster','worker','done')", (job,))
            app.DB.execute("INSERT INTO reputation(name,jobs_done) VALUES('worker',100)")
        self.assertEqual(403, self.api("POST", "/v1/presence", {"name": "worker"}, token="old-leaked")[0])
        self.assertEqual(403, self.api("POST", "/v1/registry", {"name": "worker", "endpoint": "https://x.invalid"}, token="old-leaked")[0])
        self.assertEqual([], self.api("GET", "/v1/reputation/worker")[1]["records"])
        self.assertEqual(409, self.post("/v1/jobs/" + job + "/rate", {"poster": "poster", "stars": 5}, "poster")[0])
        self.assertTrue(self.api("GET", "/v1/jobs/" + job)[1]["legacy"])

    def test_retired_name_cannot_inherit_history(self):
        self.assertEqual(403, self.api("DELETE", "/v1/registry/worker", {"secret": self.tokens["worker"]})[0])
        self.assertEqual(200, self.api("DELETE", "/v1/registry/worker", {}, "worker")[0])
        self.assertEqual(409, self.api("POST", "/v1/registry", {"name": "worker", "endpoint": "https://x.invalid"})[0])
        self.create()
        self.assertEqual(409, self.api("DELETE", "/v1/registry/poster", {}, "poster")[0])

    def test_machine_discovery_describes_actual_contract(self):
        schema = self.api("GET", "/openapi.json")[1]
        post = schema["paths"]["/v1/jobs"]["post"]
        self.assertIn("acceptance_criteria", post["requestBody"]["content"]["application/json"]["schema"]["required"])
        self.assertTrue(post["security"])
        self.assertIn("/v1/jobs/{id}/accept", schema["paths"])
        self.assertIn("submitted", schema["paths"]["/v1/jobs"]["get"]["parameters"][0]["schema"]["enum"])
        manifest = self.api("GET", "/")[1]
        self.assertTrue(any(e["path"] == "/v1/jobs/{id}/accept" for e in manifest["endpoints"]))
        card = self.api("GET", "/.well-known/agent-card.json")[1]
        self.assertIn("Bearer", card["note"])
        self.assertEqual(schema["info"]["version"], card["version"])

    def test_documented_fixture_executes_end_to_end(self):
        result = run_fixture(self.base)
        self.assertEqual("accepted", result["status"])
        self.assertEqual(["open", "claimed", "submitted", "accepted", "rated"], [event["kind"] for event in result["events"]])
        stats = self.api("GET", "/v1/stats")[1]
        self.assertEqual({"accepted": 1}, stats["task_outcomes"])

    def test_resident_patrol_is_authenticated_idempotent_and_never_accepts(self):
        job = self.create()
        self.submit(job)
        from pathlib import Path
        with patch.object(a2a_hub_client, "PUBLIC_BASE", self.base), patch.object(a2a_hub_client, "CREDENTIAL_FILE", Path(self.directory.name) / "resident.credentials.json"), patch.object(a2a_hub_client, "STATE_FILE", Path(self.directory.name) / "patrol.json"):
            first = a2a_hub_client.run_luna_welcome()
            second = a2a_hub_client.run_luna_welcome()
        self.assertEqual(1, len(first["notices_sent"]))
        self.assertEqual([], second["notices_sent"])
        self.assertEqual("submitted", self.api("GET", "/v1/jobs/" + job)[1]["status"])
        self.assertEqual(1, app.DB.execute("SELECT COUNT(*) FROM messages WHERE mtype='task_follow_up'").fetchone()[0])

    def test_presence_mail_semantics_and_browser_entrypoints(self):
        for path in ("/", "/openapi.json", "/.well-known/agent-card.json", "/v1/stats"):
            request = urllib.request.Request(self.base + path, headers={"Accept": "text/html"})
            with urllib.request.urlopen(request) as response:
                self.assertEqual(200, response.status)
        with app.DB_LOCK, app.DB:
            app.DB.execute("INSERT INTO messages(id,box,sender,body,expires) VALUES('old','poster','worker','expired',0)")
        self.assertEqual(0, self.api("GET", "/v1/inbox/poster")[1]["count"])
        self.assertEqual(0, self.api("GET", "/v1/stats")[1]["messages_unacked"])
        agent = self.api("GET", "/v1/registry/worker")[1]
        self.assertEqual(agent["online"], agent["heartbeat_recent"])
        self.assertIn("unverified", agent["presence_basis"])

    def test_pilot_accepts_exact_extraction_and_rejects_availability_claim(self):
        snapshots = [{"source_url": "https://example.invalid/card", "card": {"name": "fixture", "skills": [{"id": "z"}, {"id": "a"}]}}]
        result = expected_result(snapshots)
        self.assertTrue(verify(snapshots, result))
        result["cards"][0]["endpoint_availability"] = "verified"
        self.assertFalse(verify(snapshots, result))
        result = self.post("/v1/jobs", pilot_task(snapshots, "poster"), "poster")
        self.assertEqual(200, result[0], result)

    def test_credential_recovery_revokes_old_token_and_keeps_identity(self):
        name = "worker"
        identity = self.api("GET", "/v1/registry/worker")[1]["agent_id"]
        token = "a" * 64  # deterministic test fixture, never a deployment credential
        with app.DB_LOCK:
            recover_agent.recover(app.DB_PATH, name, token)
        self.assertEqual(403, self.api("POST", "/v1/presence", {"name": name}, name)[0])
        self.assertEqual(200, self.api("POST", "/v1/presence", {"name": name}, token=token)[0])
        self.assertEqual(identity, self.api("GET", "/v1/registry/worker")[1]["agent_id"])
        with self.assertRaises(ValueError):
            recover_agent.recover(app.DB_PATH, "unknown", token)
        with self.assertRaises(ValueError):
            recover_agent.recover(app.DB_PATH, name, "weak")
        with self.assertRaises(sqlite3.OperationalError):
            recover_agent.recover(os.path.join(self.directory.name, "missing.db"), name, token)

    def test_evidence_and_terminal_transitions_cannot_be_bypassed(self):
        job = self.create()
        self.claim(job)
        path = "/v1/jobs/" + job
        body = {"worker": "worker", "result": {"amount": 42}, "artifact": "inline:result", "evidence": ["fixture"]}
        for extra in ({"result": None}, {"artifact": ""}, {"evidence": []}):
            self.assertEqual(400, self.post(path + "/complete", dict(body, **extra), "worker")[0])
        self.assertEqual(200, self.post(path + "/complete", body, "worker")[0])
        self.assertEqual(403, self.post(path + "/accept", {"poster": "worker", "reason": "self acceptance"}, "worker")[0])
        self.assertEqual(400, self.post(path + "/accept", {"poster": "poster"}, "poster")[0])
        self.assertEqual(200, self.post(path + "/accept", {"poster": "poster", "reason": "verified"}, "poster")[0])
        self.assertEqual(409, self.post(path + "/reject", {"poster": "poster", "reason": "changed mind"}, "poster")[0])
        self.assertEqual(409, self.post(path + "/complete", body, "worker")[0])
        for stars in (True, 1.5, "5", 0, 6):
            self.assertEqual(400, self.post(path + "/rate", {"poster": "poster", "stars": stars}, "poster")[0])


class MigrationTests(unittest.TestCase):
    def test_old_schema_migration_is_repeatable_and_preserves_legacy(self):
        with tempfile.TemporaryDirectory() as directory:
            app.DB_PATH = os.path.join(directory, "old.db")
            db = sqlite3.connect(app.DB_PATH)
            db.executescript("CREATE TABLE agents(name TEXT PRIMARY KEY,agent_id TEXT,secret TEXT,endpoint TEXT,capabilities TEXT,description TEXT,registered_at TEXT,updated_at TEXT); INSERT INTO agents(name,secret) VALUES('old','exposed');")
            db.close()
            for _ in range(2):
                app.db_init()
                self.assertEqual(("exposed", 0), app.DB.execute("SELECT secret,credential_version FROM agents WHERE name='old'").fetchone())
                self.assertIn("contract", [c[1] for c in app.DB.execute("PRAGMA table_info(jobs)")])
                app.DB.close()


if __name__ == "__main__":
    unittest.main()
