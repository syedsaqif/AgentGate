# AgentGate Customer and Ops shell

Run these commands in Windows PowerShell from the repository root. The package uses Node.js 24 and npm 11.

## Mock mode

```powershell
Set-Location frontend
npm ci
npm run dev:mock
```

Open `http://127.0.0.1:5173/customer` or `http://127.0.0.1:5173/ops`. Both show the **Mock mode · Fixture data** indicator.

| Workspace | Disposable identity | Disposable secret | Successful read |
| --- | --- | --- | --- |
| Customer | `cust_001` | PIN `4821` | Masked checking account, biller catalog, and available agent |
| Ops | `ops_demo` | Password `agentgate-demo` | Registered agents and their statuses |

To reproduce an invalid sign-in, sign out and submit `cust_001` with the old PIN `0426`. The alert includes the message, `E_AUTH_INVALID_TOKEN`, and a `req_mock_…` request ID. Reloading either workspace requires sign-in again.

## Real mode

Stop the mock server with Ctrl+C. From `frontend/`, set the gateway origin and start Vite.

```powershell
$env:VITE_GATEWAY_ORIGIN = 'http://127.0.0.1:8000'
npm run dev:real
```

The browser calls the configured gateway directly. For a cross-origin setup, the gateway must allow the frontend origin, JSON POSTs, and the `Authorization` and `Content-Type` request headers via CORS. The real workspace starts with blank credential fields. An invalid configuration or failed fetch appears in the workspace. When building for real deployment, set the same variable before running `npm run build`. The static host must serve `index.html` for the two workspace routes.

## Verification

```powershell
npm run typecheck
npm test -- --run
npm run build
```

See [API notes](src/api/README.md) for provisional wire fields and [fixture notes](src/mocks/README.md) for the mock scenarios.
