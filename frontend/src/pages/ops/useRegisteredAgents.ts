import { useCallback, useEffect, useRef, useState } from "react";
import type { ShellApi } from "../../api/client";
import { ApiError } from "../../api/errors";
import type { AgentCursor, AgentMutation } from "../../api/opsWire";
import type { RegisteredAgents } from "../../api/shellWire";

export function useRegisteredAgents(api: ShellApi, token: string, endSession: (error: ApiError) => void) {
  const [page, setPage] = useState<RegisteredAgents | null>(null);
  const cursor = useRef<AgentCursor>(null);
  const [loading, setLoading] = useState(true);
  const [readError, setReadError] = useState<ApiError>();
  const [mutationError, setMutationError] = useState<ApiError>();
  const [message, setMessage] = useState("");
  const [pendingAgent, setPendingAgent] = useState<string>();
  const [stale, setStale] = useState<ReadonlySet<string>>(new Set());
  const read = useRef<AbortController | null>(null);
  const mutation = useRef<AbortController | null>(null);

  const load = useCallback(async (next: AgentCursor, replaceRead = false) => {
    if (mutation.current || (read.current && !replaceRead)) return;
    read.current?.abort();
    const controller = new AbortController();
    read.current = controller;
    setLoading(true);
    try {
      const result = await api.registeredAgents(token, controller.signal, next);
      if (read.current !== controller) return;
      cursor.current = next;
      setPage(result);
      setReadError(undefined);
      setStale((previous) => {
        const unresolved = new Set(previous);
        for (const agent of result.items) unresolved.delete(agent.agent_id);
        return unresolved;
      });
    } catch (failure) {
      if (read.current !== controller) return;
      const error = toApiError(failure);
      if (error.status === 401) endSession(error);
      else setReadError(error);
    } finally {
      if (read.current === controller) {
        read.current = null;
        setLoading(false);
      }
    }
  }, [api, token, endSession]);

  const refresh = useCallback((replaceRead = false) => void load(cursor.current, replaceRead), [load]);

  useEffect(() => {
    let cancelled = false;
    queueMicrotask(() => { if (!cancelled) refresh(); });
    return () => {
      cancelled = true;
      read.current?.abort();
      read.current = null;
      mutation.current?.abort();
      mutation.current = null;
    };
  }, [refresh]);

  async function update(id: string, name: string, action: AgentMutation) {
    if (mutation.current || stale.has(id)) return;
    const controller = new AbortController();
    mutation.current = controller;
    read.current?.abort();
    read.current = null;
    setLoading(false);
    setPendingAgent(id);
    setMutationError(undefined);
    setMessage("");
    let needsConfirmation = false;
    try {
      await api.updateAgent(token, id, action, controller.signal);
      if (mutation.current !== controller) return;
      needsConfirmation = true;
      setMessage(action === "suspend"
        ? `Global suspension acknowledged for ${name}.`
        : `Reinstatement acknowledged for ${name} for future actions.`);
    } catch (failure) {
      if (mutation.current !== controller) return;
      const error = toApiError(failure);
      if (error.status === 401) return endSession(error);
      setMutationError(error);
      needsConfirmation = error.status === undefined || error.status >= 500
        || (error.status >= 200 && error.status < 300);
      if (needsConfirmation) setMessage(`Acknowledgement unavailable for ${name}.`);
    } finally {
      if (mutation.current === controller) {
        mutation.current = null;
        setPendingAgent(undefined);
        if (needsConfirmation) {
          setStale((previous) => new Set([...previous, id]));
          refresh();
        }
      }
    }
  }

  return { page, cursor: cursor.current, loading, readError, message, pendingAgent, stale, mutationError,
    load, refresh, update };
}

function toApiError(failure: unknown): ApiError {
  return failure instanceof ApiError ? failure
    : new ApiError("The agent request could not be completed.", "UI_REQUEST_FAILED");
}
