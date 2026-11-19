# D01a mock transport

The shared `contracts/fixtures/d/ui_states.json` records state categories and calls for a visible mock indicator. The B action-hash vector and A policy cases supply the sample customer, agent, account, and biller identifiers. The role credentials, display labels, and second Ops agent are local demo data.

Each transport instance keeps its own sessions and issues a bearer token that works for one role and expires after ten minutes. The default read fixtures are populated. Tests also select empty and unavailable reads and advance the mock clock to exercise expiry. These fixtures exercise the UI and transport behavior. Gateway authentication and authorization require live provider tests.

The Customer approval inbox defaults to the S9 electricity bill for ₹4,800. The approval deadline is five minutes after fixture creation, using the injected clock. Seeded variants model decision conflicts, processing and execution outcomes. Reject-and-suspend cancels matching pending approvals for the signed-in customer and agent. The fixture receipt is synthetic. See the [API notes](../api/README.md#receipt-after-http-202) for the Customer receipt lookup gap.
