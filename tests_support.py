"""Isolated HTTP/SQLite fixture shared by integration tests.

The process-global application database requires sequential test cases. Client
threads may run concurrently inside a case to exercise transaction boundaries.
"""
import json
import os
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer
from unittest.mock import patch
import app


class HubTestCase(unittest.TestCase):
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
