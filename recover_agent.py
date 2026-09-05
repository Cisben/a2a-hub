"""Offline credential recovery after the operator independently confirms ownership.

Stop the service first. The old secret is not evidence of ownership.
Choose a new random 64-character hex token outside this program and enter it at
the hidden prompt. Only its SHA-256 digest is written to the database.
"""
import argparse
import getpass
import hashlib
import re
import sqlite3
from pathlib import Path


def recover(database, name, token):
    if not re.fullmatch(r"[0-9a-f]{64}", token):
        raise ValueError("Use a fresh randomly generated 64-character lowercase hex token")
    uri = Path(database).resolve().as_uri() + "?mode=rw"
    with sqlite3.connect(uri, uri=True) as db:
        row = db.execute("SELECT 1 FROM agents WHERE name=?", (name,)).fetchone()
        if not row:
            raise ValueError("Unknown agent; recovery never creates names")
        db.execute("UPDATE agents SET secret=?,credential_version=1 WHERE name=?",
                   (hashlib.sha256(token.encode()).hexdigest(), name))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("database")
    parser.add_argument("name")
    args = parser.parse_args()
    token = getpass.getpass("New secret (64 random hex characters): ")
    if token != getpass.getpass("Confirm new secret: "):
        raise SystemExit("Secrets do not match")
    recover(args.database, args.name, token)
    print("Credential replaced. Deliver the secret through your private recovery channel.")


if __name__ == "__main__":
    main()
