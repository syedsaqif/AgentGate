import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../../api/errors";
import { bankApi } from "./bankApi";
import { addBankNotice, decodeBankNotice, type BankNotice, type Overview } from "./bankWire";

export type Mutation = (perform: (signal: AbortSignal) => Promise<string | void>) => Promise<boolean>;
export const sessionExpired = "Your session has expired. Sign in again.";
const messageFor = (error: unknown) => error instanceof ApiError ? error.message : "The bank could not complete the request.";

export function useBankWorkspace(initial: Overview, endSession: (message?: string) => void) {
  const [overview, setOverview] = useState(initial);
  const [notices, setNotices] = useState<BankNotice[]>([]);
  const [connection, setConnection] = useState<"connecting" | "live" | "recovering">("connecting");
  const [readError, setReadError] = useState("");
  const [actionError, setActionError] = useState("");
  const [feedback, setFeedback] = useState("");
  const [busy, setBusy] = useState(false);
  const lifetime = useRef<AbortController | null>(null);
  const read = useRef<Promise<void> | null>(null);
  const requested = useRef(false);
  const writing = useRef(false);
  const stale = useRef(false);
  const { customerId, accountId } = initial;

  useEffect(() => {
    const controller = new AbortController();
    lifetime.current = controller;
    return () => {
      controller.abort();
      read.current = null;
    };
  }, []);

  const refresh = useCallback((): Promise<void> => {
    const signal = lifetime.current?.signal;
    if (!signal || signal.aborted) return Promise.resolve();
    requested.current = true;
    if (read.current) return read.current;
    const task = Promise.resolve().then(async () => {
      while (requested.current && !signal.aborted) {
        requested.current = false;
        try {
          const latest = await bankApi.overview(signal);
          if (signal.aborted) return;
          if (latest.customerId !== customerId || latest.accountId !== accountId) {
            endSession(sessionExpired);
            return;
          }
          setOverview(latest);
          stale.current = false;
          setReadError("");
        } catch (error) {
          if (signal.aborted) return;
          if (error instanceof ApiError && error.status === 401) {
            endSession(sessionExpired);
            return;
          }
          stale.current = true;
          setReadError(messageFor(error));
        }
      }
    }).finally(() => { if (read.current === task) read.current = null; });
    read.current = task;
    return task;
  }, [accountId, customerId, endSession]);

  const mutate: Mutation = async (perform) => {
    const signal = lifetime.current?.signal;
    if (!signal || signal.aborted || writing.current || stale.current) return false;
    writing.current = true;
    setBusy(true);
    setActionError("");
    setFeedback("");
    try {
      const result = await perform(signal);
      if (signal.aborted) return false;
      if (result) setFeedback(result);
      await refresh();
      return !signal.aborted;
    } catch (error) {
      if (signal.aborted) return false;
      if (error instanceof ApiError && error.status === 401) endSession(sessionExpired);
      else {
        setActionError(messageFor(error));
        await refresh();
      }
      return false;
    } finally {
      writing.current = false;
      if (!signal.aborted) setBusy(false);
    }
  };

  useEffect(() => {
    const controller = new AbortController();
    let bookmark: string | undefined;
    let retry: number | undefined;
    let activeStream: AbortController | undefined;

    async function connect() {
      if (controller.signal.aborted || activeStream || !navigator.onLine) return;
      const stream = new AbortController();
      activeStream = stream;
      const close = () => stream.abort();
      controller.signal.addEventListener("abort", close, { once: true });
      let expired = false;
      try {
        await bankApi.stream(bookmark, stream.signal, () => {
          setConnection("live");
          void refresh();
        }, (frame) => {
          if (frame.event === "session.expired" || frame.event === "reconnect") {
            expired = frame.event === "session.expired";
            stream.abort();
            return;
          }
          if (frame.event === "resync") {
            bookmark = undefined;
            void refresh();
            return;
          }
          if (frame.id && /^\d{1,20}$/.test(frame.id)) bookmark = frame.id;
          const notice = decodeBankNotice(frame, { customerId, accountId });
          if (!notice) return;
          setNotices((previous) => addBankNotice(previous, notice));
          void refresh();
        });
      } catch (error) {
        expired ||= error instanceof ApiError && error.status === 401;
      } finally {
        controller.signal.removeEventListener("abort", close);
        activeStream = undefined;
      }
      if (controller.signal.aborted) return;
      if (expired) {
        endSession(sessionExpired);
        return;
      }
      setConnection("recovering");
      if (navigator.onLine) void refresh();
      retry = window.setTimeout(() => void connect(), 1_500);
    }

    function offline() {
      window.clearTimeout(retry);
      setConnection("recovering");
      activeStream?.abort();
    }

    function online() {
      window.clearTimeout(retry);
      void connect();
      void refresh();
    }

    window.addEventListener("offline", offline);
    window.addEventListener("online", online);
    void connect();
    return () => {
      controller.abort();
      window.clearTimeout(retry);
      window.removeEventListener("offline", offline);
      window.removeEventListener("online", online);
    };
  }, [accountId, customerId, endSession, refresh]);

  const hasOpenApproval = overview.approvals.some(({ state, expires_at }) => state === "APPROVED"
    || state === "PENDING" && Date.parse(expires_at) > Date.now());
  useEffect(() => {
    if (!hasOpenApproval) return;
    const timer = window.setInterval(() => { if (navigator.onLine) void refresh(); }, 3_000);
    return () => window.clearInterval(timer);
  }, [hasOpenApproval, refresh]);

  async function signOut() {
    const signal = lifetime.current?.signal;
    if (!signal || writing.current) return;
    writing.current = true;
    setBusy(true);
    try {
      await bankApi.logout(signal);
      if (!signal.aborted) endSession();
    } catch (error) {
      if (!signal.aborted) setActionError(messageFor(error));
    } finally {
      writing.current = false;
      if (!signal.aborted) setBusy(false);
    }
  }

  return { overview, notices, connection, readError, actionError, feedback, busy, refresh, mutate, signOut };
}
