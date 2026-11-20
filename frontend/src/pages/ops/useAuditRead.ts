import { useCallback, useEffect, useRef, useState } from "react";
import { ApiError } from "../../api/errors";

type AuditRead<T> = { kind: "idle" | "loading" } | { kind: "ready"; data: T } | { kind: "error"; error: ApiError };

export function useAuditRead<T>(endSession: (error: ApiError) => void) {
  const [state, setState] = useState<AuditRead<T>>({ kind: "idle" });
  const current = useRef<AbortController | null>(null);
  const cancel = useCallback(() => {
    current.current?.abort();
    current.current = null;
  }, []);

  useEffect(() => cancel, [cancel]);

  const clear = useCallback(() => {
    cancel();
    setState({ kind: "idle" });
  }, [cancel]);

  const run = useCallback(async (read: (signal: AbortSignal) => Promise<T>) => {
    cancel();
    const controller = new AbortController();
    current.current = controller;
    setState({ kind: "loading" });
    try {
      const data = await read(controller.signal);
      if (current.current === controller) setState({ kind: "ready", data });
    } catch (failure) {
      if (current.current !== controller) return;
      const error = failure instanceof ApiError
        ? failure : new ApiError("The audit request could not be completed.", "UI_REQUEST_FAILED");
      if (error.status === 401) endSession(error);
      else setState({ kind: "error", error });
    } finally {
      if (current.current === controller) current.current = null;
    }
  }, [cancel, endSession]);

  return { state, run, clear };
}
