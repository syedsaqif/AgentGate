import { useEffect, useRef, useState } from "react";
import { readSse } from "../../api/sse";
import { gateFeedReducer, emptyFeed, type FeedAction } from "./gateTimeline";
import { decodeGateFrame } from "./gateWire";

type Connection = "connecting" | "live" | "reconnecting" | "sign-in";
const streamUrl = import.meta.env.VITE_DEMO_GATE_STREAM_URL?.trim() || "http://127.0.0.1:8002/demo/gate/stream";

export function useGateStream(enabled: boolean, epoch = 0) {
  const [feed, setFeed] = useState(emptyFeed);
  const [connection, setConnection] = useState<Connection>("connecting");
  const [attempt, setAttempt] = useState(0);
  const state = useRef(emptyFeed);

  useEffect(() => {
    state.current = emptyFeed;
    setFeed(emptyFeed);
  }, [epoch, enabled]);

  useEffect(() => {
    if (!enabled) return;
    const controller = new AbortController();
    const { signal } = controller;
    let retry: number | undefined;
    let retryDelay = 500;

    function update(action: FeedAction) {
      // Track the bookmark synchronously even when React batches replay frames.
      state.current = gateFeedReducer(state.current, action);
      setFeed(state.current);
    }

    async function connect() {
      if (signal.aborted) return;
      try {
        const headers = new Headers({ Accept: "text/event-stream" });
        if (state.current.cursor) headers.set("Last-Event-ID", state.current.cursor);
        const response = await fetch(streamUrl, { headers, credentials: "include", signal });
        if (signal.aborted) return;
        if (response.status === 401 || response.status === 403) {
          update({ type: "clear" });
          setConnection("sign-in");
          return;
        }
        if (!response.ok || !response.body || !response.headers.get("Content-Type")?.startsWith("text/event-stream")) {
          throw new Error("Gate stream unavailable");
        }
        setConnection("live");
        retryDelay = 500;
        const stream = new AbortController();
        const abortStream = () => stream.abort();
        signal.addEventListener("abort", abortStream, { once: true });
        let sessionExpired = false;
        try {
          await readSse(response.body, stream.signal, (frame) => {
            if (frame.event === "resync") {
              update({ type: "resync" });
              return;
            }
            if (frame.event === "reconnect" || frame.event === "error" || frame.event === "session.expired") {
              sessionExpired = frame.event === "session.expired";
              stream.abort();
              return;
            }
            const cursor = frame.id && /^[0-9]{1,20}$/.test(frame.id) ? frame.id : undefined;
            const event = decodeGateFrame(frame);
            if (cursor || event) update({ type: "frame", cursor, event });
          });
        } finally {
          signal.removeEventListener("abort", abortStream);
        }
        if (signal.aborted) return;
        if (sessionExpired) {
          update({ type: "clear" });
          setConnection("sign-in");
          return;
        }
      } catch {
        if (signal.aborted) return;
      }
      setConnection("reconnecting");
      retry = window.setTimeout(() => void connect(), retryDelay);
      retryDelay = Math.min(retryDelay * 2, 5_000);
    }

    setConnection("connecting");
    void connect();
    return () => {
      controller.abort();
      window.clearTimeout(retry);
    };
  }, [enabled, attempt, epoch]);

  return { feed, connection, reconnect: () => setAttempt((previous) => previous + 1) };
}
