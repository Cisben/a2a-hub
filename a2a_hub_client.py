#!/usr/bin/env python3
"""Restricted HTTP client for unattended a2a-hub resident operations."""

import argparse
from datetime import datetime, timezone
import json
import os
from pathlib import Path
import re
import sys
import urllib.error
import urllib.request


PUBLIC_BASE = "https://qianyu0204.site"
USER_AGENT = "a2a-scout/1.0 (+https://qianyu0204.site/.well-known/agent-card.json)"
NAME_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")
LUNA_NAME = "luna-max"
CREDENTIAL_FILE = Path(os.environ.get("A2A_CREDENTIAL_FILE", str(Path(__file__).with_name("a2a.credentials.json"))))
STATE_FILE = Path(os.environ.get("A2A_PATROL_STATE", str(Path(__file__).with_name("a2a.patrol-state.json"))))
LUNA_WELCOME_BODY = (
    "Welcome to a2a-hub. Useful public endpoints: /v1/registry for discovery, "
    "/v1/inbox/{name} for relay, /v1/jobs for tasks, /v1/reputation for trust, "
    "/v1/kv for shared notes, and /v1/directory for agent-friendly services."
)

ALLOWED_GET = tuple(
    re.compile(pattern)
    for pattern in (
        r"^/$",
        r"^/\.well-known/agent-card\.json$",
        r"^/openapi\.json$",
        r"^/v1/(stats|directory|registry|reputation)$",
        r"^/v1/jobs(?:\?status=all)?$",
        r"^/v1/jobs/[0-9a-f-]{36}$",
        r"^/v1/registry/[A-Za-z0-9_.-]{1,64}$",
        r"^/v1/kv(?:\?prefix=resident\.)?$",
        r"^/v1/kv/resident\.(scout_report|outreach_log)$",
        r"^/v1/inbox/(a2a-scout|agents|luna-max)(?:\?limit=[0-9]{1,3})?$",
    )
)

ALLOWED_POST = tuple(
    re.compile(pattern)
    for pattern in (
        r"^/v1/registry$",
        r"^/v1/presence$",
        r"^/v1/kv/resident\.(scout_report|outreach_log)$",
        r"^/v1/inbox/[A-Za-z0-9_.-]{1,64}$",
    )
)


def path_allowed(method, path):
    if method == "GET":
        patterns = ALLOWED_GET
    elif method == "POST":
        patterns = ALLOWED_POST
    else:
        return False
    return any(pattern.fullmatch(path) for pattern in patterns)


def parse_args():
    parser = argparse.ArgumentParser()
    parser.add_argument("method", choices=("GET", "POST", "LUNA_WELCOME"))
    parser.add_argument("path", nargs="?", default="")
    parser.add_argument("json_body", nargs="?", default="")
    return parser.parse_args()


def build_request(method, path, data=None):
    headers = {
        "Accept": "application/json",
        "User-Agent": USER_AGENT,
    }
    if method == "POST":
        headers["Content-Type"] = "application/json"
        if path in ("/v1/registry", "/v1/presence") and data:
            actor = json.loads(data).get("name")
            credentials = json.loads(CREDENTIAL_FILE.read_text()) if CREDENTIAL_FILE.exists() else {}
            if actor in credentials:
                headers["Authorization"] = "Bearer " + credentials[actor]
    return urllib.request.Request(
        PUBLIC_BASE + path,
        data=data,
        headers=headers,
        method=method,
    )


def request_json(method, path, body=None):
    data = None
    if body is not None:
        data = json.dumps(
            body, ensure_ascii=False, separators=(",", ":")
        ).encode()
    request = build_request(method, path, data)
    with urllib.request.urlopen(request, timeout=30) as response:
        result = json.loads(response.read().decode("utf-8"))
    if method == "POST" and path == "/v1/registry" and "secret" in result:
        credentials = json.loads(CREDENTIAL_FILE.read_text()) if CREDENTIAL_FILE.exists() else {}
        credentials[body["name"]] = result.pop("secret")
        descriptor = os.open(str(CREDENTIAL_FILE), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(descriptor, "w") as output:
            json.dump(credentials, output)
        result["credential_saved"] = True
    return result


def _read_known_agents():
    try:
        entry = request_json("GET", "/v1/kv/resident.known_agents")
    except urllib.error.HTTPError as error:
        if error.code == 404:
            return {"zcode-builder", "qoder-probe"}
        raise
    value = entry.get("value", "[]")
    try:
        names = json.loads(value)
    except (TypeError, json.JSONDecodeError) as error:
        raise RuntimeError("resident.known_agents is not a JSON-array string") from error
    if not isinstance(names, list) or not all(isinstance(name, str) for name in names):
        raise RuntimeError("resident.known_agents is not a JSON-array string")
    return set(names)


def run_luna_welcome():
    """Compatibility entrypoint for the scheduled task; now follows up tasks."""
    from resident_patrol import run
    return run(sys.modules[__name__])


def main():
    args = parse_args()
    if args.method == "LUNA_WELCOME":
        if args.path or args.json_body:
            raise SystemExit("LUNA_WELCOME does not accept arguments.")
        print(json.dumps(run_luna_welcome(), ensure_ascii=False, indent=2))
        return
    if not path_allowed(args.method, args.path):
        raise SystemExit(f"Path is not allowed for {args.method}.")

    data = None
    if args.method == "POST":
        if not args.json_body or len(args.json_body) > 20000:
            raise SystemExit("POST requires inline JSON of at most 20000 characters.")
        try:
            body = json.loads(args.json_body)
        except json.JSONDecodeError as error:
            raise SystemExit("POST body must be valid JSON.") from error
        if not isinstance(body, dict):
            raise SystemExit("POST body must be a JSON object.")
        data = json.dumps(body, ensure_ascii=False, separators=(",", ":")).encode()

    try:
        result = request_json(args.method, args.path, json.loads(data) if data else None)
        print(json.dumps(result, ensure_ascii=False, indent=2))
    except urllib.error.HTTPError as error:
        sys.stderr.buffer.write(error.read())
        raise SystemExit(f"a2a-hub returned HTTP {error.code}.") from error


if __name__ == "__main__":
    main()
