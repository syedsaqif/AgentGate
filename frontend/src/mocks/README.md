# D01a mock transport

The shared `contracts/fixtures/d/ui_states.json` records state categories and calls for a visible mock indicator. The B action-hash vector and A policy cases supply the sample customer, agent, account, and biller identifiers. The role credentials, display labels, and second Ops agent are local demo data.

Each transport instance keeps its own sessions and issues a bearer token that works for one role and expires after ten minutes. The default read fixtures are populated. Tests also select empty and unavailable reads and advance the mock clock to exercise expiry. These fixtures exercise the UI and transport behavior. Gateway authentication and authorization require live provider tests.

The Customer approval inbox defaults to the S9 electricity bill for ₹4,800. The approval deadline is five minutes after fixture creation, using the injected clock. Seeded variants model decision conflicts, processing and execution outcomes. Reject-and-suspend cancels matching pending approvals for the signed-in customer and agent. The fixture receipt is synthetic. See the [API notes](../api/README.md#receipt-after-http-202) for the Customer receipt lookup gap.

The Ops fixtures emit deterministic decision and registry events as SSE through the same transport and decoder used in real mode. Each instance keeps its registry and event IDs in memory. A known `Last-Event-ID` resumes after that event. An unknown ID emits a resync notification and replays available fixture events. Separate tests use simulated fetch streams to split frames, disconnect and exercise recovery timers.

The Ops audit fixtures return redacted rows, synthetic S11 bundles with historical agent snapshots, and deterministic S10 reports. Their first failure sequences and reason codes follow B's unmerged `tests/b/test_b07_verify_evidence.py`. These fixtures exercise display and download behavior. The gateway verifier and tamper tools provide the integrity checks.
