import { useCallback, useEffect, useRef, useState, type FormEvent } from "react";
import { ApiError } from "../../../api/errors";
import { readAndClearPin } from "../../../components/pin";
import { bankApi } from "./bankApi";
import { BankHome } from "./BankHome";
import { BankMandates } from "./BankMandates";
import type { Overview } from "./bankWire";
import { useBankWorkspace } from "./useBankWorkspace";
import "./demoBank.css";

type EndSession = (message?: string) => void;
const connectionLabels = { live: "Live updates", connecting: "Connecting", recovering: "Reconnecting" };

function SignedInBank({ initial, endSession }: { initial: Overview; endSession: EndSession }) {
  const bank = useBankWorkspace(initial, endSession);
  const [screen, setScreen] = useState<"home" | "mandates">("home");
  const disabled = bank.busy || !!bank.readError;

  return (
    <>
      <div className="bank-toolbar">
        <span className="bank-connection" data-connection={bank.connection} role="status">{connectionLabels[bank.connection]}</span>
        <button type="button" className="bank-secondary" disabled={bank.busy} onClick={() => void bank.refresh()}>Refresh</button>
        <button type="button" className="bank-secondary" disabled={bank.busy} onClick={() => void bank.signOut()}>Sign out</button>
      </div>
      <nav className="bank-nav" aria-label="Bank screens">
        <button type="button" aria-current={screen === "home" ? "page" : undefined} onClick={() => setScreen("home")}>Home</button>
        <button type="button" aria-current={screen === "mandates" ? "page" : undefined} onClick={() => setScreen("mandates")}>Mandates</button>
      </nav>
      {bank.readError && <p className="bank-error" role="alert">Account data could not be refreshed. Showing the last bank snapshot.</p>}
      {bank.actionError && <p className="bank-error" role="alert">{bank.actionError}</p>}
      {bank.feedback && <p className="bank-feedback" role="status">{bank.feedback}</p>}
      <div hidden={screen !== "home"}>
        <BankHome overview={bank.overview} notices={bank.notices} disabled={disabled} mutate={bank.mutate} />
      </div>
      {screen === "mandates" && <BankMandates overview={bank.overview} disabled={disabled} mutate={bank.mutate} />}
    </>
  );
}

export default function DemoBankPage() {
  const [checking, setChecking] = useState(true);
  const [initial, setInitial] = useState<Overview>();
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState("");
  const [customerId, setCustomerId] = useState("cust_001");
  const loginRequest = useRef<AbortController | null>(null);

  const endSession = useCallback<EndSession>((message) => {
    setInitial(undefined);
    setError(message ?? "");
  }, []);

  useEffect(() => {
    const controller = new AbortController();
    void bankApi.overview(controller.signal).then((overview) => {
      if (!controller.signal.aborted) {
        setCustomerId(overview.customerId);
        setInitial(overview);
      }
    }).catch((failure: unknown) => {
      if (!controller.signal.aborted && failure instanceof ApiError && failure.status !== 401) setError(failure.message);
    }).finally(() => { if (!controller.signal.aborted) setChecking(false); });
    return () => { controller.abort(); loginRequest.current?.abort(); };
  }, []);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const pin = readAndClearPin(event.currentTarget);
    if (loginRequest.current || !pin || !customerId.trim()) return;
    const controller = new AbortController();
    loginRequest.current = controller;
    setError("");
    setBusy(true);
    try {
      await bankApi.login(customerId.trim(), pin, controller.signal);
      const overview = await bankApi.overview(controller.signal);
      if (!controller.signal.aborted) setInitial(overview);
    } catch (failure) {
      if (!controller.signal.aborted) setError(failure instanceof ApiError ? failure.message : "Could not sign in.");
    } finally {
      if (loginRequest.current === controller) loginRequest.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  return (
    <main className="bank-demo">
      <header className="bank-header">
        <span className="bank-mark" aria-hidden="true"><i /><i /><i /></span>
        <div><h1>Demo Bank</h1><p>AgentGate</p></div>
      </header>
      {checking ? <p className="bank-card" role="status">Checking session…</p>
        : initial ? <SignedInBank initial={initial} endSession={endSession} />
        : <section className="bank-card bank-login" aria-labelledby="bank-sign-in">
          <h2 id="bank-sign-in">Sign in</h2>
          {error && <p className="bank-error" role="alert">{error}</p>}
          <form className="bank-form" method="post" onSubmit={(event) => void signIn(event)}>
            <fieldset disabled={busy}>
              <label>Customer ID<input name="customer_id" autoComplete="username" maxLength={64}
                value={customerId} onChange={(event) => setCustomerId(event.target.value)} required /></label>
              <label>Customer PIN<input name="pin" type="password" inputMode="numeric" autoComplete="off"
                minLength={4} maxLength={16} required /></label>
              <button type="submit">{busy ? "Signing in…" : "Sign in"}</button>
            </fieldset>
          </form>
        </section>}
    </main>
  );
}
