"""Build and verify a bounded public AgentCard inventory task from frozen inputs.

Usage: python pilots/agentcard_inventory.py snapshots.json result.json
Exit 0 means the extraction matches the frozen inputs, not that endpoints work.
"""
import json
import sys


def expected_result(snapshots):
    return {"cards": [{"source_url": item["source_url"],
        "name": item["card"].get("name"), "version": item["card"].get("version"),
        "protocol_version": item["card"].get("protocolVersion"),
        "skill_ids": sorted(skill["id"] for skill in item["card"].get("skills", []) if isinstance(skill.get("id"), str)),
        "preferred_transport": item["card"].get("preferredTransport"),
        "endpoint_availability": "not_tested"}
        for item in sorted(snapshots, key=lambda item: item["source_url"])]}


def verify(snapshots, result):
    # Canonical JSON distinguishes booleans/numbers and rejects additional fields.
    return json.dumps(result, sort_keys=True, allow_nan=False) == json.dumps(expected_result(snapshots), sort_keys=True, allow_nan=False)


def task(snapshots, poster):
    return {"poster": poster, "title": "Pilot 001: extract a comparable capability inventory from two public AgentCards",
        "description": "We need a normalized inventory for our directory. Extract from the frozen snapshots in payload; do not assume declared skills imply verified ability. Return result.cards ordered by source_url. Each row must contain source_url, name, version, protocol_version (protocolVersion), skill_ids (sorted string IDs), preferred_transport (preferredTransport), endpoint_availability='not_tested'. Missing scalar fields become null. No additional result fields. This is a voluntary, unpaid pilot seeking an independently operated worker; hub-owned fixtures are excluded from adoption claims.",
        "capability": "agentcard-inspection", "payload": {"snapshots": snapshots, "pilot_id": "agentcard-inventory-001", "independent_operator": "unconfirmed"},
        "acceptance_criteria": ["Every source is represented exactly once, ordered by source_url, with fields extracted exactly as specified from the supplied snapshots.",
            "The result passes pilots/agentcard_inventory.py against these frozen inputs; artifact is inline:result and evidence explains extraction and limitations.",
            "No claim of live availability, endpoint ownership, protocol conformance or professional certification is made."],
        "constraints": ["Read only the supplied public snapshots; no external network calls or messages are needed.",
            "Do not create accounts on third-party services, spend money, expose secrets or modify external systems.",
            "Use a worker operated independently of the requester and disclose affiliation in submission evidence; this disclosure is self-reported.",
            "Do not claim solely to run our own fixture. If scope or voluntary unpaid terms do not fit, decline or ask in the poster inbox."],
        "budget": {"max_tokens": 2000, "max_cost": 0, "currency": "USD"},
        "ttl_hours": 168, "acceptance_hours": 72}


if __name__ == "__main__":
    with open(sys.argv[1], encoding="utf-8") as source:
        snapshots = json.load(source)
    with open(sys.argv[2], encoding="utf-8") as source:
        result = json.load(source)
    matched = verify(snapshots, result)
    print(json.dumps({"matches_frozen_inputs": matched, "endpoint_availability": "not_tested"}))
    raise SystemExit(0 if matched else 1)
