import { useCallback, useEffect, useRef, useState, type FormEvent, type ReactNode } from "react";
import type { ShellApi } from "../api/client";
import { ApiError } from "../api/errors";
import type { HumanSession, Role } from "../api/shellWire";
import { ErrorNotice } from "./ErrorNotice";

type WorkspaceProps<T> = {
  role: Role;
  api: ShellApi;
  demoId?: string;
  read: (token: string, signal: AbortSignal) => Promise<T>;
  renderData: (data: T, session: HumanSession, clearSession: (error?: ApiError) => void) => ReactNode;
};

type View<T> =
  | { kind: "signIn"; error?: ApiError }
  | { kind: "signingIn" }
  | { kind: "loading"; session: HumanSession }
  | { kind: "ready"; session: HumanSession; data: T }
  | { kind: "readError"; session: HumanSession; error: ApiError };

function expiredSession() {
  return new ApiError("Your session has expired. Sign in again.", "UI_SESSION_EXPIRED");
}

export function RoleWorkspace<T>({ role, api, demoId, read, renderData }: WorkspaceProps<T>) {
  const [view, setView] = useState<View<T>>({ kind: "signIn" });
  const activeRequest = useRef<AbortController | null>(null);
  const session = "session" in view ? view.session : undefined;

  const clearSession = useCallback((error?: ApiError) => {
    activeRequest.current?.abort();
    activeRequest.current = null;
    setView({ kind: "signIn", error });
  }, []);

  useEffect(() => () => activeRequest.current?.abort(), []);

  useEffect(() => {
    if (!session) return;

    let timer: number;
    const expiresAt = Date.parse(session.expires_at);
    function checkExpiry() {
      window.clearTimeout(timer);
      const remaining = expiresAt - Date.now();
      if (remaining <= 0) clearSession(expiredSession());
      else timer = window.setTimeout(checkExpiry, Math.min(remaining, 2_147_483_647));
    }

    function checkRestoredPage(event: PageTransitionEvent) {
      if (event.persisted) clearSession();
    }

    checkExpiry();
    window.addEventListener("focus", checkExpiry);
    window.addEventListener("pageshow", checkRestoredPage);
    return () => {
      window.clearTimeout(timer);
      window.removeEventListener("focus", checkExpiry);
      window.removeEventListener("pageshow", checkRestoredPage);
    };
  }, [clearSession, session]);

  async function signIn(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (activeRequest.current) return;

    const form = event.currentTarget;
    const fields = new FormData(form);
    const id = String(fields.get("identity") ?? "").trim();
    const secret = String(fields.get("secret") ?? "");
    form.reset();

    const controller = new AbortController();
    const { signal } = controller;
    activeRequest.current = controller;
    setView({ kind: "signingIn" });

    let issuedSession: HumanSession | undefined;
    try {
      issuedSession = await api.login(role, id, secret, signal);
      if (signal.aborted) return;
      const expiresAt = Date.parse(issuedSession.expires_at);
      if (expiresAt <= Date.now()) return clearSession(expiredSession());

      setView({ kind: "loading", session: issuedSession });
      const data = await read(issuedSession.session_token, signal);
      if (signal.aborted) return;
      if (expiresAt <= Date.now()) return clearSession(expiredSession());
      setView({ kind: "ready", session: issuedSession, data });
    } catch (failure) {
      if (signal.aborted) return;
      const error = failure instanceof ApiError
        ? failure
        : new ApiError("The request could not be completed.", "UI_REQUEST_FAILED");
      if (issuedSession && error.status !== 401) {
        setView({ kind: "readError", session: issuedSession, error });
      } else {
        clearSession(error);
      }
    } finally {
      if (activeRequest.current === controller) activeRequest.current = null;
    }
  }

  if (session) {
    return (
      <>
        <div className="session-bar">
          <p>Signed in as {role === "customer" ? "Customer" : "Ops"}</p>
          <button type="button" className="secondary" onClick={() => clearSession()}>Sign out</button>
        </div>
        {view.kind === "loading" && <p className="panel" role="status">Loading workspace…</p>}
        {view.kind === "readError" && <ErrorNotice error={view.error} />}
        {view.kind === "ready" && renderData(view.data, view.session, clearSession)}
      </>
    );
  }

  const isCustomer = role === "customer";
  return (
    <section className="panel sign-in" aria-labelledby="sign-in-title">
      <h2 id="sign-in-title">{isCustomer ? "Customer sign-in" : "Ops sign-in"}</h2>
      {view.kind === "signIn" && view.error && <ErrorNotice error={view.error} />}
      <form method="post" onSubmit={signIn} autoComplete="off">
        <fieldset disabled={view.kind === "signingIn"}>
          <label>
            {isCustomer ? "Customer ID" : "Operator ID"}
            <input name="identity" defaultValue={demoId} required spellCheck={false} />
          </label>
          <label>
            {isCustomer ? "PIN" : "Password"}
            <input name="secret" type="password" inputMode={isCustomer ? "numeric" : undefined} required />
          </label>
          <button type="submit">Sign in</button>
        </fieldset>
      </form>
      {view.kind === "signingIn" && <p role="status">Signing in…</p>}
    </section>
  );
}
