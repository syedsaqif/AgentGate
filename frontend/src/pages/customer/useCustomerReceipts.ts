import { useCallback, useEffect, useRef, useState } from "react";
import { isReceipt, type AuditReceipt } from "../../api/approvalWire";
import type { ShellApi } from "../../api/client";
import { ApiError } from "../../api/errors";
import { isRecord } from "../../api/shellWire";

export function useCustomerReceipts(api: ShellApi, token: string, endSession: (error: ApiError) => void) {
  const tracked = useRef(new Set<string>());
  const track = useCallback((id: string) => { tracked.current.add(id); }, []);
  const [receipts, setReceipts] = useState(new Map<string, AuditReceipt>());
  const [connection, setConnection] = useState("Connecting to Customer receipt stream…");

  useEffect(() => {
    const controller = new AbortController();
    const { signal } = controller;
    let bookmark: string | undefined;
    let timer: ReturnType<typeof setTimeout>;
    async function connect() {
      try {
        await api.streamCustomer(token, bookmark, signal, () => setConnection("Customer receipt stream connected."), (frame) => {
          if (signal.aborted) return;
          if (frame.event === "resync") {
            bookmark = frame.id || undefined;
            setConnection("Live receipts resumed. Earlier receipts may be unavailable.");
            return;
          }
          if (!frame.id) return;
          bookmark = frame.id;
          if (frame.event !== "bank.executed") return;
          let event: unknown;
          try { event = JSON.parse(frame.data); } catch { return; }
          if (!isRecord(event) || event.event_id !== frame.id || event.event_type !== frame.event
            || typeof event.request_id !== "string" || !tracked.current.has(event.request_id)
            || !isRecord(event.data) || event.data.status !== "EXECUTED" || !isReceipt(event.data.audit_ref)) return;
          const id = event.request_id;
          const receipt = event.data.audit_ref;
          setReceipts((previous) => new Map(previous).set(id, receipt));
        });
      } catch (error) {
        if (signal.aborted) return;
        if (error instanceof ApiError && error.status === 401) return endSession(error);
        if (error instanceof ApiError && error.status === 404) {
          setConnection("Customer receipt stream unavailable.");
          return;
        }
      }
      if (!signal.aborted) {
        setConnection("Receipt stream disconnected. Missed receipts may be unavailable.");
        timer = setTimeout(() => void connect(), 2000);
      }
    }
    void connect();
    return () => { controller.abort(); clearTimeout(timer); };
  }, [api, token, endSession]);
  return { receipts, track, connection };
}
