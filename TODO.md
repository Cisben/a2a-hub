# Task trust TODO

This iteration keeps the hub an account-free public coordination service.
Acceptance records describe the requester's decision, not platform certification.

- [x] Authenticate registry updates, presence and task mutations; quarantine legacy credentials.
- [x] Separate immutable task contracts, submission and requester acceptance/rejection.
- [x] Commit task transitions, events, notifications and idempotency receipts atomically.
- [x] Retain failures/timeouts and report per-capability outcomes without treating legacy completions as accepted.
- [x] Update machine discovery, migration guidance and a runnable two-agent example.
- [x] Test authorization, replay/conflicts, concurrency, migration and lifecycle; prepare CI template.
- [ ] Enable `ci/tests.yml` at `.github/workflows/tests.yml` (GitHub token needs `workflow` scope; push was rejected).
- [ ] Open a PR with migration and compatibility notes.

Follow-up product experiments (outside this PR):

- Recruit two independent operators for a small, objectively verifiable task.
- Measure connection time, acceptance cost and repeat use; separate fixtures from real adoption.
- Add AgentCard conformance/freshness checks before expanding directory ingestion.
- Add capability challenges once actual task failures identify useful test cases.
- Consider scoped attestations only after repeatable evidence exists.
