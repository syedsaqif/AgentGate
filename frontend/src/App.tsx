import { BootstrapNotice } from "./components/BootstrapNotice";
import { CustomerPage } from "./pages/customer/CustomerPage";
import { OpsPage } from "./pages/ops/OpsPage";

export function App() {
  const isOps = window.location.pathname.startsWith("/ops");
  return (
    <main>
      <header>
        <p className="eyebrow">AgentGate · contract 1.0.0</p>
        <h1>{isOps ? "Operations workspace" : "Customer workspace"}</h1>
      </header>
      <BootstrapNotice />
      {isOps ? <OpsPage /> : <CustomerPage />}
    </main>
  );
}
