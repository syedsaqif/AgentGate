import { useEffect, useState } from "react";
import type { ShellApi } from "../../api/client";
import { ApiError } from "../../api/errors";
import type { OpsDecision } from "../../api/opsWire";

const recoveryInterval = 2_000;

export function useOpsFeed(api: ShellApi, token: string, endSession: (error: ApiError) => void,
  refreshAgents: (replaceRead?: boolean) => void) {
  const [connection, setConnection] = useState<"connecting" | "live" | "recovering">("connecting");
  const [error, setError] = useState<ApiError>();
  const [historyGap, setHistoryGap] = useState(false);
  const [decisions, setDecisions] = useState<OpsDecision[]>([]);

  useEffect(() => {
    const session = new AbortController();
    const { signal } = session;
    const seen = new Set<string>();
    let lastId: string | undefined;
    let retry: number | undefined;
    let poll: number | undefined;
    let recovering = false;

    async function connect() {
      if (signal.aborted) return;
      try {
        await api.streamOps(token, lastId, signal, {
          opened() {
            if (signal.aborted) return;
            window.clearInterval(poll);
            poll = undefined;
            setConnection("live");
            setError(undefined);
            if (recovering) refreshAgents(true);
          },
          event(event) {
            if (signal.aborted) return;
            if (event.id && seen.has(event.id)) return;
            if (event.kind === "resync") {
              setHistoryGap(true);
              lastId = event.id || undefined;
              if (event.id) seen.add(event.id);
              refreshAgents(true);
              return;
            }
            seen.add(event.id);
            lastId = event.id;
            if (event.kind === "decision") setDecisions((previous) => [event.decision, ...previous].slice(0, 50));
            else if (event.refreshAgents) refreshAgents(true);
          },
        });
      } catch (failure) {
        if (signal.aborted) return;
        if (failure instanceof ApiError && failure.status === 401) return endSession(failure);
        setError(failure instanceof ApiError ? failure : new ApiError("The Ops stream disconnected.", "UI_STREAM_DISCONNECTED"));
      }
      if (signal.aborted) return;
      recovering = true;
      setConnection("recovering");
      setHistoryGap(true);
      if (poll === undefined) poll = window.setInterval(() => refreshAgents(), recoveryInterval);
      retry = window.setTimeout(() => void connect(), recoveryInterval);
    }

    queueMicrotask(() => void connect());
    return () => {
      session.abort();
      window.clearTimeout(retry);
      window.clearInterval(poll);
    };
  }, [api, token, endSession, refreshAgents]);

  return { connection, error, historyGap, decisions };
}
