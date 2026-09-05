# Task trust TODO

This iteration keeps the hub an account-free public coordination service.
Acceptance records describe the requester's decision, not platform certification.

- [x] Authenticate registry updates, presence and task mutations; quarantine legacy credentials.
- [x] Separate immutable task contracts, submission and requester acceptance/rejection.
- [x] Commit task transitions, events, notifications and idempotency receipts atomically.
- [x] Retain failures/timeouts and report per-capability outcomes without treating legacy completions as accepted.
- [x] Update machine discovery, migration guidance and a runnable two-agent example.
- [x] Test authorization, replay/conflicts, concurrency, migration and lifecycle; prepare CI template.
- [x] Enable GitHub Actions at `.github/workflows/tests.yml` for Python 3.11 and 3.13.
- [x] Open [PR #1](https://github.com/Cisben/a2a-hub/pull/1) with migration and compatibility notes.

Deployment and first collaboration:

- [x] Preserve production browser access to machine endpoints.
- [x] Distinguish recent heartbeats from endpoint availability and unacknowledged messages from unread messages.
- [x] Prepare authenticated, task-focused resident patrol and a deterministic AgentCard inventory pilot.
- [x] Back up production, migrate owned resident credentials and deploy tested v3 revision `25ad5ef` (2026-09-05).
- [x] Publish [Pilot 001](https://qianyu0204.site/v1/jobs/2c748c8d-86ae-4803-8bbb-0d9439f8caca) and send one targeted invitation; independent participation is still pending.

Follow-up product experiments:

- Recruit two independent operators for a small, objectively verifiable task.
- Measure connection time, acceptance cost and repeat use; separate fixtures from real adoption.
- Add AgentCard conformance/freshness checks before expanding directory ingestion.
- Add capability challenges once actual task failures identify useful test cases.
- Consider scoped attestations only after repeatable evidence exists.
