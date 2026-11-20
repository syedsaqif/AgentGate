# AgentGate seven-minute demo

Owner: Member D. Timing follows the R&D plan §7.5. Scenario values and acceptance criteria follow the [team distribution §13.2](../TEAM_TASK_DISTRIBUTION%20%2828%29.md).

## Before the timer

Use an integrated real checkout that has passed the D06 smoke and the required scenarios. Start from the disposable setup in the [README](../README.md). In the live browser use the gateway run configuration from the rehearsal. The automated smoke exits and cleans up its stack, so have the integrated team's interactive launcher and seeded accounts ready for the presentation. Use fresh isolated data for each scenario, including an untouched audit copy for S11. Queue the verified scenario commands and mark the output panes with the scenario IDs. Use a disposable copy and a retained checkpoint for S10. If a provider or scenario is still blocked, show the status slide and say which result is pending. Keep mock practice out of the real rehearsal count.

**Screen layout.** Keep the Customer browser on the left at `/customer` with Accounts, Biller catalog, Available agents, the approval inbox and mandates. Keep a separate Ops session on the right at `/ops` with Live decision feed, Registered agents and Audit. Zoom each active workspace to make the amount, decision and receipt readable. Keep a terminal ready for signed requests and ledger checks. Enter PINs off the projector and keep credentials, tokens and private keys out of terminal output and recordings.

## Presenter script

| Time | Speaker cue | Screen or terminal action | Expected visible result and verification |
| --- | --- | --- | --- |
| 0:00–0:30 | “If an agent can pay a bill, what stops it from paying the wrong person?” | Show title and a customer electricity bill. | Audience sees the authorized bill and the question of who controls the transfer. |
| 0:30–1:30 | “The agent signs a request. AgentGate checks identity and mandate, evaluates risk, asks the customer where needed, and records the decision before calling the bank with authority.” | Show Agent → AgentGate → simulated bank. Point to Customer approval and Ops audit beside the gateway. | Five capabilities are visible on the slide: identity, scoped mandates, risk, human approval, and audit/evidence. Customer and Ops act through the gateway. |
| 1:30–3:00 | “I grant BillPal permission to pay this electricity bill within a cap and a budget.” | In Customer, show the agent fingerprint, allowed payee, caps and expiry. Confirm the grant, submit the signed S1 request, then show the correlated Ops event and provider balance read. | **Live pending.** ₹1,850 executes once. Balance moves from 12,500,000 to 12,315,000 paise and remaining mandate budget from 1,000,000 to 815,000. |
| 3:00–4:00 | “An over-limit request and a copied token both fail before dispatch.” | Run prepared S2a, S6b and S6c checks. Show the denial reason codes in the terminal and Ops. | **Live pending.** ₹7,000 is denied by the per-transaction cap. Wrong-key use and proof replay are denied. |
| 4:00–5:15 | “An instruction hidden in a bill cannot widen the mandate. Repeated smaller payments also change the risk.” | Show S7’s attempted transfer and suspension in Ops, then switch to the isolated S8 run and show its ordered events. | **Live pending.** The ₹40,000 transfer to unverified PAYEE-X is denied and the agent suspended. In S8, ₹1,990 attempts show the first allow, next two pending and fourth denied with suspension under the tuned fixture. Pending approvals cancel and at most one payment executes. |
| 5:15–5:45 | “This ₹4,800 request needs a person. I’m approving the exact payee and amount on the card.” | Switch to the staged S9a pending item. Point to the structured payee, amount and risk reason. Approve once and show the correlated executed receipt. | **Live pending.** The approved stored action executes once and the receipt carries the same request ID. For S9b, show a separately verified rejection with zero debit if its evidence is ready. |
| 5:45–6:15 | “The customer can withdraw authority immediately.” | Revoke the relevant mandate in Customer, show the refreshed status, then submit the prepared next agent call in the S5 scenario. | **Live pending.** The mandate shows revoked, the next call fails and pending approvals cancel. |
| 6:15–6:45 | “Verification points to the first broken entry. The dispute bundle tells us exactly what was authorized at the time.” | Show Ops Verify on the S10 disposable tamper copy. Switch to the clean Ops tab, filter by the S9a request ID and open or download the S11 evidence. | **Live pending.** First failure and reason appear on the copy. S11 shows the historical mandate, public agent identity and signed proof, risk and approval details, bank outcome, and coverage scope. Match the receipt to the audit references. |
| 6:45–7:00 | “Five controls, one enforcement point. Here is the evidence we have today.” | Show the results slide and end at 7:00. | The audience sees the observed checks and the remaining live gates. |

**Total** 420 seconds.

## Results slide

| Observed | Pending measurement or verification |
| --- | --- |
| D06 frontend suite passed 114 tests across 10 files. The D07 Customer and Ops mock sign-ins also passed. | Live D06 payment and receipt, one correlated bank debit and balance delta, and integrated S1–S11 results. |
| Prior D06 smoke exited 1. The simulated bank started and the browser rendered both workspaces. Real gateway readiness failed. | Real startup, live user flow, three real rehearsals, an independent clean-reset run, and a successful recording. |
| Separate simulated-bank idempotency and S9 fixture balance tests passed. | Race overspends, local p95 decision latency, verification time for 1,000 entries, and coverage of the four anchored tamper variants. Report measurements with revision, seed and configuration. |

If a real run passes, update the slide from the [verification log](TEST_RUN.md). Keep the successful backup recording outside Git and put its retrievable location in that log.
