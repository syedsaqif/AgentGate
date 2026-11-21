import { lazy, Suspense } from "react";
import type { ShellApi } from "./api/client";
import type { ApiError } from "./api/errors";
import type { Role } from "./api/shellWire";
import { CONTRACT_VERSION } from "./api/types";
import { ErrorNotice } from "./components/ErrorNotice";
import { CustomerPage } from "./pages/customer/CustomerPage";
import { OpsPage } from "./pages/ops/OpsPage";
import { HomePage } from "./pages/home/HomePage";

const PresenterStage = lazy(() => import("./pages/demo/stage/PresenterStage"));
const DemoGatePage = lazy(() => import("./pages/demo/DemoGatePage"));
const DemoBankPage = lazy(() => import("./pages/demo/bank/DemoBankPage"));
const DemoAgentPage = lazy(() => import("./pages/demo/agent/DemoAgentPage"));

type AppProps =
  | { mode: "mock"; api: ShellApi; demoIds: Record<Role, string> }
  | { mode: "real"; api: ShellApi }
  | { mode: "real"; error: ApiError };

export function App(props: AppProps) {
  const path = window.location.pathname.replace(/\/$/, "");
  if (path === "" || path === "/home") {
    return <HomePage />;
  }
  if (path === "/demo") {
    return <Suspense fallback={<main>Loading presenter stage…</main>}><PresenterStage /></Suspense>;
  }
  if (path === "/demo/gate") {
    return <Suspense fallback={<main>Loading payment gate…</main>}><DemoGatePage /></Suspense>;
  }
  if (path === "/demo/bank") {
    return <Suspense fallback={<main>Loading bank…</main>}><DemoBankPage /></Suspense>;
  }
  if (path === "/demo/agent") {
    return <Suspense fallback={<main>Loading assistant…</main>}><DemoAgentPage /></Suspense>;
  }
  const isOps = path === "/ops";
  const demoIds = props.mode === "mock" ? props.demoIds : undefined;
  return (
    <main>
      <header className="site-header">
        <div>
          <p className="brand">AgentGate</p>
          <p className="version">Contract {CONTRACT_VERSION}</p>
        </div>
        <nav aria-label="Workspaces">
          <a href="/customer" aria-current={!isOps ? "page" : undefined}>Customer</a>
          <a href="/ops" aria-current={isOps ? "page" : undefined}>Ops</a>
        </nav>
      </header>
      <h1>{isOps ? "Operations workspace" : "Customer workspace"}</h1>
      {props.mode === "mock" && <p className="mock-mode">Mock mode · Fixture data</p>}
      {"error" in props ? <ErrorNotice error={props.error} /> : isOps
        ? <OpsPage api={props.api} demoId={demoIds?.ops} />
        : <CustomerPage api={props.api} demoId={demoIds?.customer} />}
    </main>
  );
}
