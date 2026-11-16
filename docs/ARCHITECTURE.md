# Architecture boundaries

AgentGate is one FastAPI gateway with in-process modules, not one network service per teammate. It uses one gateway SQLite database in WAL mode with one worker and a separate SQLite database for the loopback-only mock bank. The React/Vite client exposes Customer and Ops routes. Integer paise and `INR` are the only money representation; timestamps are UTC and displayed in `Asia/Kolkata`.

## Dependency direction

Implementation modules may import only shared types from `contracts/`. A's composition root will construct real providers and inject them through typed ports. A module must not import a neighbor's repository, mutate a neighbor's tables, or pass ORM models across a boundary.

```text
frontend / agent_sdk / simulator
              |
       single FastAPI gateway
              |
 identity -> policy -> risk -> decision -> audit -> execution
              |                               |
       gateway SQLite                   private BankClient
                                              |
                                      loopback mock bank
                                      separate SQLite DB
```

Development fakes model both success and negative outcomes. They are allowed in `test`, forbidden in `scripted` for security modules, and forbidden in `real`. Passing a mock contract suite never proves cryptographic, concurrency, persistence, or tamper-resistance guarantees.

## Database write ownership

- A: mandates, mandate usage, reservations, actions, idempotency; all mock-bank tables.
- B: customers, human sessions, agents, issued tokens, audit log.
- C: escalations, risk attempts, risk baselines.
- D: no server database writes.

The caller supplies an opaque transaction context when a state change spans ports. Services do not commit caller-owned transactions. No network call occurs while holding a SQLite writer transaction.
