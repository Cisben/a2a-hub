"""Regression tests for real delivery failures and multi-author workflows."""
import json
import os
import sqlite3
import tempfile
import time
import unittest
import urllib.error
import urllib.request
import uuid
from concurrent.futures import ThreadPoolExecutor
from unittest.mock import patch

import app
import messaging
import open_calls
from tests_support import HubTestCase

ARTIFACT = {"url": "https://example.org/result.html", "media_type": "text/html", "size_bytes": 123}


class CollaborationTests(HubTestCase):
    def create_call(self, **extra):
        status, data = self.post("/v1/calls", {
            "poster": "poster", "title": "Two independent designs", "description": "Public fixture",
            "acceptance_criteria": ["Provide a complete artifact reference"], **extra,
        }, "poster")
        self.assertEqual(200, status, data)
        return "/v1/calls/" + data["call_id"]

    def contribute(self, path, actor="worker", key=None):
        return self.post(path + "/submissions", {"author": actor, "summary": "Design",
                         "artifacts": [ARTIFACT], "evidence": ["Public fixture only"]}, actor, key)

    def raw(self, method, path, body=None, headers=None):
        request = urllib.request.Request(self.base + path, method=method,
            data=json.dumps(body).encode() if body is not None else None,
            headers={"Content-Type": "application/json", **(headers or {})})
        try:
            response = urllib.request.urlopen(request, timeout=5)
        except urllib.error.HTTPError as error:
            response = error
        with response:
            return response.status, json.loads(response.read())

    def test_unicode_boundary_rejects_without_truncation_or_partial_box(self):
        good = "中" * 8192
        self.assertEqual(200, self.api("POST", "/v1/inbox/good", {"body": good})[0])
        self.assertEqual(good, self.api("GET", "/v1/inbox/good")[1]["messages"][0]["body"])
        status, result = self.api("POST", "/v1/inbox/never-created", {"body": good + "界", "key": "key"})
        self.assertEqual(413, status)
        self.assertEqual("message_too_large", result["error"])
        self.assertIsNone(app.DB.execute("SELECT 1 FROM boxes WHERE box='never-created'").fetchone())
        self.assertEqual(1, app.DB.execute("SELECT v FROM counters WHERE k='messages_relayed'").fetchone()[0])

    def test_full_inbox_does_not_evict_existing_work(self):
        self.api("POST", "/v1/inbox/full", {"body": "important"})
        with patch.object(messaging, "MAX_PENDING", 1):
            self.assertEqual(429, self.api("POST", "/v1/inbox/full", {"body": "new"})[0])
        self.assertEqual(["important"], [m["body"] for m in self.api("GET", "/v1/inbox/full")[1]["messages"]])

    def test_sender_verification_requires_credentials_and_idempotency(self):
        path = "/v1/inbox/reader"
        self.assertEqual(200, self.api("POST", path, {"sender": "worker", "body": "anonymous claim"})[0])
        self.assertEqual(403, self.post(path, {"sender": "worker", "body": "forged"}, "intruder")[0])
        self.assertEqual(400, self.api("POST", path, {"sender": "worker", "body": "no key"}, actor="worker")[0])
        body = {"sender": "worker", "body": "authenticated"}
        first = self.post(path, body, "worker", "retry-mail")
        self.assertEqual(first, self.post(path, body, "worker", "retry-mail"))
        self.assertEqual(409, self.post(path, dict(body, body="different"), "worker", "retry-mail")[0])
        records = self.api("GET", path + "?cursor=0")[1]["messages"]
        self.assertEqual([False, True], [r["sender_verified"] for r in records])
        self.assertEqual(2, len(records))

    def test_authenticated_mail_concurrent_replay_and_restart(self):
        body = {"sender": "worker", "body": "one delivery"}
        with ThreadPoolExecutor(max_workers=2) as pool:
            results = list(pool.map(lambda _: self.post("/v1/inbox/mail", body, "worker", "once"), range(2)))
        self.assertEqual(results[0], results[1])
        queued = results[0][1]["queued"]
        self.api("POST", "/v1/inbox/mail/ack", {"ids": [queued]})
        with app.DB_LOCK:
            app.DB.close()
            app.db_init()
        self.assertEqual(results[0], self.post("/v1/inbox/mail", body, "worker", "once"))
        self.assertEqual(0, self.api("GET", "/v1/inbox/mail")[1]["count"])

    def test_cursor_does_not_skip_same_second_messages_or_reuse_deleted_ids(self):
        with patch.object(app, "now_iso", return_value="2026-01-01T00:00:00+00:00"):
            for index in range(3):
                self.api("POST", "/v1/inbox/page", {"body": str(index)})
        first = self.api("GET", "/v1/inbox/page?cursor=0&limit=2")[1]
        self.assertEqual(["0", "1"], [m["body"] for m in first["messages"]])
        self.assertTrue(first["has_more"])
        second = self.api("GET", "/v1/inbox/page?cursor=" + str(first["next_cursor"]))[1]
        self.assertEqual(["2"], [m["body"] for m in second["messages"]])
        cursor = second["next_cursor"]
        self.api("POST", "/v1/inbox/page/ack", {"ids": [m["id"] for m in first["messages"] + second["messages"]]})
        with app.DB_LOCK:
            app.DB.close()
            app.db_init()
        self.api("POST", "/v1/inbox/page", {"body": "after restart"})
        later = self.api("GET", "/v1/inbox/page?cursor=" + str(cursor))[1]
        self.assertEqual("after restart", later["messages"][0]["body"])
        self.assertGreater(later["next_cursor"], cursor)
        self.assertEqual(later["next_cursor"], self.api("GET", "/v1/inbox/page?cursor=" + str(later["next_cursor"]))[1]["next_cursor"])

    def test_query_bounds_and_mixed_pagination_fail_cleanly(self):
        for query in ("limit=-1", "limit=101", "cursor=-1", "cursor=nan", "wait=nan", "wait=inf", "wait=-1", "cursor=0&after=x", "cursor=" + "9"*30):
            self.assertEqual(400, self.api("GET", "/v1/inbox/box?" + query)[0], query)

    def test_reply_preserves_context_without_cross_box_read_access(self):
        parent = self.api("POST", "/v1/inbox/private", {"body": "parent", "key": "read-key", "conversation_id": "topic"})[1]["queued"]
        body = {"body": "reply", "reply_to": parent}
        self.assertEqual(404, self.api("POST", "/v1/inbox/public", body)[0])
        self.assertEqual(403, self.api("GET", "/v1/inbox/private?conversation_id=topic")[0])
        status, reply = self.raw("POST", "/v1/inbox/public", body, {"X-Reply-Box-Key": "read-key"})
        self.assertEqual(200, status)
        self.assertEqual("topic", reply["conversation_id"])
        self.assertEqual(409, self.raw("POST", "/v1/inbox/public", dict(body, conversation_id="wrong"), {"X-Reply-Box-Key": "read-key"})[0])
        self.raw("POST", "/v1/inbox/private/ack", {"ids": [parent]}, {"X-Box-Key": "read-key"})
        self.assertEqual(404, self.raw("POST", "/v1/inbox/public", body, {"X-Reply-Box-Key": "read-key"})[0])
        self.assertEqual(200, self.api("POST", "/v1/inbox/public", {"body": "continue", "conversation_id": "topic"})[0])

    def test_artifact_metadata_roundtrips_and_context_must_exist(self):
        path = self.create_call()
        body = {"body": "file", "call_id": path.rsplit("/", 1)[-1], "artifacts": [ARTIFACT]}
        self.assertEqual(200, self.api("POST", "/v1/inbox/files", body)[0])
        stored = self.api("GET", "/v1/inbox/files")[1]["messages"][0]
        self.assertEqual([ARTIFACT], stored["artifacts"])
        for invalid in ({"url": "file:///etc/passwd"}, {"url": "https://user:pass@example.org/x"},
                        {"url": "https://example.org:bad/x"}, {"size_bytes": True},
                        {"sha256": "not-a-digest"}, {"media_type": "text/html\nBad: x"}):
            self.assertEqual(400, self.api("POST", "/v1/inbox/files", dict(body, artifacts=[dict(ARTIFACT, **invalid)]))[0])
        self.assertEqual(404, self.api("POST", "/v1/inbox/files", dict(body, call_id=str(uuid.uuid4())))[0])

    def test_two_authors_receive_independent_decisions_and_durable_events(self):
        path = self.create_call()
        worker = self.contribute(path)[1]["submission_id"]
        other = self.contribute(path, "intruder")[1]["submission_id"]
        self.assertEqual(409, self.contribute(path)[0])
        for submission, decision in ((worker, "accept"), (other, "reject")):
            self.assertEqual(200, self.post(path + "/submissions/" + submission + "/" + decision,
                {"poster": "poster", "reason": "Checked the fixture"}, "poster")[0])
        call = self.api("GET", path)[1]
        self.assertEqual("open", call["status"])
        self.assertEqual({"accepted", "rejected"}, {s["status"] for s in call["submissions"]})
        self.assertEqual(["open", "submitted", "submitted", "accepted", "rejected"], [e["kind"] for e in call["events"]])
        with app.DB_LOCK, app.DB:
            app.DB.execute("DELETE FROM messages")
        self.assertEqual(call, self.api("GET", path)[1])
        self.assertEqual([], self.api("GET", "/v1/reputation")[1]["records"])
        self.assertEqual({"accepted": 1, "rejected": 1}, self.api("GET", "/v1/stats")[1]["submission_outcomes"])

    def test_calls_cannot_be_impersonated_or_self_submitted(self):
        path = self.create_call()
        self.assertEqual(400, self.contribute(path, "poster")[0])
        submission = self.contribute(path)[1]["submission_id"]
        decision = path + "/submissions/" + submission + "/accept"
        self.assertEqual(403, self.post(decision, {"poster": "poster", "reason": "forged"}, "intruder")[0])
        self.assertEqual(403, self.post(decision, {"poster": "intruder", "reason": "wrong role"}, "intruder")[0])
        self.assertEqual(400, self.post(decision, {"poster": "poster", "reason": ""}, "poster")[0])
        another = self.create_call()
        self.assertEqual(404, self.post(another + "/submissions/" + submission + "/accept", {"poster": "poster", "reason": "wrong call"}, "poster")[0])

    def test_close_stops_new_submissions_but_allows_multiple_acceptances(self):
        path = self.create_call()
        submissions = [self.contribute(path, actor)[1]["submission_id"] for actor in ("worker", "intruder")]
        self.assertEqual(200, self.post(path + "/close", {"poster": "poster", "reason": "Enough proposals"}, "poster")[0])
        self.assertEqual(409, self.contribute(path)[0])
        for submission in submissions:
            self.assertEqual(200, self.post(path + "/submissions/" + submission + "/accept", {"poster": "poster", "reason": "Useful"}, "poster")[0])
        self.assertEqual(["accepted", "accepted"], [s["status"] for s in self.api("GET", path)[1]["submissions"]])

    def test_call_and_review_deadlines_are_independent(self):
        path = self.create_call()
        submission = self.contribute(path)[1]["submission_id"]
        with app.DB_LOCK, app.DB:
            app.DB.execute("UPDATE calls SET expires=0")
        self.assertEqual("expired", self.api("GET", path)[1]["status"])
        self.assertEqual(409, self.contribute(path, "intruder")[0])
        self.assertEqual("submitted", self.api("GET", path)[1]["submissions"][0]["status"])
        with app.DB_LOCK, app.DB:
            app.DB.execute("UPDATE call_submissions SET acceptance_due=0")
        self.assertEqual(409, self.post(path + "/submissions/" + submission + "/accept", {"poster": "poster", "reason": "late"}, "poster")[0])
        self.assertEqual("acceptance_expired", self.api("GET", path)[1]["submissions"][0]["status"])

    def test_submission_receipt_survives_decision_restart_and_conflicts(self):
        path = self.create_call()
        first = self.contribute(path, key="stable")
        submission = first[1]["submission_id"]
        self.post(path + "/submissions/" + submission + "/accept", {"poster": "poster", "reason": "ok"}, "poster")
        with app.DB_LOCK:
            app.DB.close()
            app.db_init()
        self.assertEqual(first, self.contribute(path, key="stable"))
        self.assertEqual(409, self.post("/v1/inbox/worker", {"sender": "worker", "body": "reuse"}, "worker", "stable")[0])
        self.assertEqual("accepted", self.api("GET", path)[1]["submissions"][0]["status"])

    def test_submission_rollback_does_not_consume_retry_key(self):
        path = self.create_call()
        with patch.object(open_calls, "notify", side_effect=RuntimeError("injected delivery failure")):
            self.assertEqual(500, self.contribute(path, key="retry")[0])
        self.assertEqual(0, app.DB.execute("SELECT COUNT(*) FROM call_submissions").fetchone()[0])
        self.assertEqual(0, app.DB.execute("SELECT COUNT(*) FROM task_receipts WHERE actor='worker'").fetchone()[0])
        self.assertEqual(1, len(self.api("GET", path)[1]["events"]))
        self.assertEqual(200, self.contribute(path, key="retry")[0])

    def test_concurrent_decisions_have_one_winner(self):
        path = self.create_call()
        submission = self.contribute(path)[1]["submission_id"]
        def decide(action):
            return self.post(path + "/submissions/" + submission + "/" + action,
                             {"poster": "poster", "reason": action}, "poster")[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            self.assertEqual([200, 409], sorted(pool.map(decide, ("accept", "reject"))))

    def test_full_inbox_does_not_block_durable_submission(self):
        path = self.create_call()
        self.api("POST", "/v1/inbox/poster", {"body": "keep me"})
        with patch.object(messaging, "MAX_PENDING", 1):
            self.assertEqual(200, self.contribute(path)[0])
        call = self.api("GET", path)[1]
        self.assertFalse(call["events"][-1]["detail"]["inbox_notification_queued"])
        self.assertEqual("keep me", self.api("GET", "/v1/inbox/poster")[1]["messages"][0]["body"])

    def test_retirement_cannot_orphan_pending_reviews(self):
        path = self.create_call()
        submission = self.contribute(path)[1]["submission_id"]
        self.post(path + "/close", {"poster": "poster", "reason": "stop"}, "poster")
        for actor in ("poster", "worker"):
            self.assertEqual(409, self.api("DELETE", "/v1/registry/" + actor, {}, actor)[0])
        self.post(path + "/submissions/" + submission + "/reject", {"poster": "poster", "reason": "done"}, "poster")
        self.assertEqual(200, self.api("DELETE", "/v1/registry/worker", {}, "worker")[0])

    def test_call_enumeration_and_unsupported_methods(self):
        paths = [self.create_call() for _ in range(3)]
        first = self.api("GET", "/v1/calls?status=all&limit=2")[1]
        second = self.api("GET", "/v1/calls?status=all&cursor=" + str(first["next_cursor"]))[1]
        self.assertTrue(first["has_more"])
        self.assertEqual(1, second["count"])
        self.assertEqual(3, len({c["call_id"] for c in first["calls"] + second["calls"]}))
        self.assertEqual(405, self.api("GET", paths[0] + "/close")[0])
        self.assertEqual(405, self.post(paths[0], {"poster": "poster"}, "poster")[0])
        self.assertEqual(400, self.api("GET", "/v1/calls?status=accepted")[0])

    def test_discovery_and_default_python_user_agent_work(self):
        schema = self.api("GET", "/openapi.json")[1]
        self.assertIn("/v1/calls/{id}/submissions/{submission_id}/accept", schema["paths"])
        self.assertIn("ArtifactReferences", schema["components"]["schemas"])
        self.assertIn("413", schema["paths"]["/v1/inbox/{box}"]["post"]["responses"])
        for path in ("/", "/.well-known/agent-card.json", "/openapi.json"):
            self.assertEqual(200, self.raw("GET", path)[0])

    def test_documented_multi_author_fixture(self):
        from examples.collaboration_roundtrip import run
        result = run(self.base)
        self.assertEqual("closed", result["status"])
        self.assertEqual(["accepted", "accepted"], result["submission_states"])
        self.assertEqual(4, result["messages_processed"])
        self.assertEqual(0, result["messages_acknowledged"])

    def test_call_submission_limits_fail_without_partial_writes(self):
        path = self.create_call()
        with patch.object(open_calls, "MAX_OPEN_CALLS", 1):
            status, _ = self.post("/v1/calls", {"poster": "poster", "title": "Another", "description": "x",
                                  "acceptance_criteria": ["x"]}, "poster")
            self.assertEqual(429, status)
        with patch.object(open_calls, "MAX_SUBMISSIONS", 1):
            self.assertEqual(200, self.contribute(path)[0])
            self.assertEqual(429, self.contribute(path, "intruder")[0])
        self.assertEqual(1, len(self.api("GET", path)[1]["submissions"]))

    def test_invalid_call_contract_does_not_consume_key(self):
        body = {"poster": "poster", "title": "x", "description": "x", "acceptance_criteria": ["x"]}
        for extra in ({"acceptance_criteria": []}, {"ttl_hours": float("nan")},
                      {"ttl_hours": True}, {"acceptance_hours": 0}, {"description": "x" * 4001}):
            self.assertEqual(400, self.post("/v1/calls", dict(body, **extra), "poster", "retry-create")[0])
        self.assertEqual(200, self.post("/v1/calls", body, "poster", "retry-create")[0])

    def test_failed_message_transaction_rolls_back_body_cursor_and_receipt(self):
        with app.DB_LOCK:
            app.DB.execute("""CREATE TRIGGER reject_mail_counter BEFORE INSERT ON counters
                WHEN NEW.k='messages_relayed' BEGIN SELECT RAISE(ABORT,'fixture counter failure'); END""")
        body = {"sender": "worker", "body": "must not partially persist"}
        self.assertEqual(500, self.post("/v1/inbox/atomic", body, "worker", "retry")[0])
        for table in ("messages", "message_cursors"):
            self.assertEqual(0, app.DB.execute("SELECT COUNT(*) FROM " + table).fetchone()[0])
        self.assertIsNone(app.DB.execute("SELECT 1 FROM boxes WHERE box='atomic'").fetchone())
        with app.DB_LOCK:
            app.DB.execute("DROP TRIGGER reject_mail_counter")
        self.assertEqual(200, self.post("/v1/inbox/atomic", body, "worker", "retry")[0])

    def test_sweeper_failure_rolls_back_expiry_and_cursor_cleanup(self):
        queued = self.api("POST", "/v1/inbox/expiry", {"body": "old"})[1]["queued"]
        with app.DB_LOCK, app.DB:
            app.DB.execute("UPDATE messages SET expires=0 WHERE id=?", (queued,))
        with patch.object(app.time, "sleep", side_effect=[None, KeyboardInterrupt]), \
                patch.object(open_calls, "expire", side_effect=RuntimeError("injected sweep failure")):
            with self.assertRaises(KeyboardInterrupt):
                app.sweep_db()
        self.assertIsNotNone(app.DB.execute("SELECT 1 FROM messages WHERE id=?", (queued,)).fetchone())
        self.assertIsNotNone(app.DB.execute("SELECT 1 FROM message_cursors WHERE message_id=?", (queued,)).fetchone())


class MessageMigrationTests(unittest.TestCase):
    def test_v3_message_data_survives_repeatable_migration(self):
        with tempfile.TemporaryDirectory() as directory:
            app.DB_PATH = os.path.join(directory, "v3.db")
            db = sqlite3.connect(app.DB_PATH)
            db.execute("CREATE TABLE messages(id TEXT PRIMARY KEY,box TEXT,sender TEXT,mtype TEXT,body TEXT,created_at TEXT,expires REAL)")
            original = [("b", "reader", "worker", "note", "完整正文", "2026-01-01", time.time()+3600),
                        ("a", "reader", "worker", "note", "same timestamp", "2026-01-01", time.time()+3600)]
            db.executemany("INSERT INTO messages VALUES(?,?,?,?,?,?,?)", original)
            db.commit()
            db.close()
            for _ in range(2):
                app.db_init()
                try:
                    self.assertEqual(original, app.DB.execute("SELECT id,box,sender,mtype,body,created_at,expires FROM messages ORDER BY id DESC").fetchall())
                    self.assertEqual([("a", 1), ("b", 2)], app.DB.execute("SELECT message_id,sequence FROM message_cursors ORDER BY sequence").fetchall())
                    self.assertEqual([(0,), (0,)], app.DB.execute("SELECT sender_verified FROM messages").fetchall())
                    self.assertEqual("ok", app.DB.execute("PRAGMA integrity_check").fetchone()[0])
                finally:
                    app.DB.close()
