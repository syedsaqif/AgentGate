# D01a mock transport

The shared `contracts/fixtures/d/ui_states.json` records state categories and calls for a visible mock indicator. The B action-hash vector and A policy cases supply the sample customer, agent, account, and biller identifiers. The role credentials, display labels, and second Ops agent are local demo data.

Each transport instance keeps its own sessions and issues a bearer token that works for one role and expires after ten minutes. The default read fixtures are populated. Tests also select empty and unavailable reads and advance the mock clock to exercise expiry. These fixtures exercise the UI and transport behavior. Gateway authentication and authorization require live provider tests.
