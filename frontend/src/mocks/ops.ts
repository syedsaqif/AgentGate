import { opsStreamPath } from "../api/opsWire";
import { shellRoutes, type RegisteredAgents } from "../api/shellWire";
import { emptyAgents, registeredAgents } from "./fixtures";

const mutationPath = /^\/v1\/ops\/agents\/([^/]+)\/(suspend|reinstate)$/;

export function isOpsRoute(path: string) {
  return path === opsStreamPath || path === shellRoutes.ops.read || mutationPath.test(path);
}

export function createOpsFixtures(now: () => number, scenario: "populated" | "empty" | "unavailable") {
  const agents = (scenario === "empty" ? emptyAgents : registeredAgents).items.map((agent) => ({ ...agent }));
  const history: { id: string; frame: string }[] = [];
  const subscribers = new Set<(frame: string) => void>();
  const encoder = new TextEncoder();

  function publish(event_type: string, agent_id: string, data: object, request_id?: string) {
    const event_id = String(history.length + 1);
    const envelope = { event_id, event_type, occurred_at: new Date(now()).toISOString(), request_id, agent_id, data };
    const frame = `id: ${event_id}\nevent: ${event_type}\ndata: ${JSON.stringify(envelope)}\n\n`;
    history.push({ id: event_id, frame });
    for (const send of subscribers) send(frame);
  }

  if (scenario !== "empty") {
    publish("action.decision", "agt_billpal", {
      decision: "ESCALATE",
      risk: { score: 50, signals: [{ message: "Amount above the approval threshold" }] },
    }, "req_ops_demo_review");
    publish("action.decision", "agt_billpal", {
      decision: "ALLOW", reason_codes: ["POLICY_ALLOWED"],
    }, "req_ops_demo_allow");
  }

  function stream(lastId: string | null, signal: AbortSignal | null | undefined) {
    let cleanup: () => void = () => undefined;
    const body = new ReadableStream<Uint8Array>({
      start(controller) {
        if (signal?.aborted) {
          controller.close();
          return;
        }
        const send = (frame: string) => controller.enqueue(encoder.encode(frame));
        const abort = () => { cleanup(); controller.close(); };
        cleanup = () => {
          subscribers.delete(send);
          signal?.removeEventListener("abort", abort);
        };
        const index = lastId === null ? -1 : history.findIndex(({ id }) => id === lastId);
        if (lastId !== null && index < 0) send("event: resync\ndata: {}\n\n");
        for (const { frame } of history.slice(index + 1)) send(frame);
        subscribers.add(send);
        signal?.addEventListener("abort", abort, { once: true });
      },
      cancel() { cleanup(); },
    });
    return new Response(body, { headers: { "Content-Type": "text/event-stream", "Cache-Control": "no-store" } });
  }

  return function handleOps(url: URL, init: RequestInit, { respond, reject }: {
    respond: (payload: unknown, status?: number) => Response;
    reject: (status: number, code: string, message: string) => Response;
  }) {
    if (url.pathname === opsStreamPath && init.method === "GET") {
      return stream(new Headers(init.headers).get("Last-Event-ID"), init.signal);
    }
    if (url.pathname === shellRoutes.ops.read && init.method === "GET") {
      if (scenario === "unavailable") return reject(503, "E_SERVICE_UNAVAILABLE", "The workspace data is temporarily unavailable.");
      const cursor = url.searchParams.get("cursor");
      const limit = Number(url.searchParams.get("limit") ?? 50);
      const start = cursor === null ? 0 : agents.findIndex((agent) => cursorFor(agent.agent_id) === cursor) + 1;
      if (!Number.isInteger(limit) || limit < 1 || limit > 100 || (cursor !== null && start === 0)) {
        return reject(400, "E_REQ_INVALID", "Invalid agent page parameters.");
      }
      const items = agents.slice(start, start + limit);
      return respond({
        items, next_cursor: start + items.length < agents.length ? cursorFor(items[items.length - 1].agent_id) : null,
      } satisfies RegisteredAgents);
    }
    const mutation = mutationPath.exec(url.pathname);
    if (mutation && init.method === "POST") {
      const agent = agents.find((row) => encodeURIComponent(row.agent_id) === mutation[1]);
      if (!agent) return reject(404, "E_REQ_INVALID", "Agent not found.");
      if (agent.status === "DISABLED") return reject(403, "E_REQ_INVALID", "This agent is disabled.");
      const suspend = mutation[2] === "suspend";
      agent.status = suspend ? "SUSPENDED" : "ACTIVE";
      publish(suspend ? "agent.suspended" : "agent.reinstated", agent.agent_id, { status: agent.status });
      return respond(agent);
    }
    return reject(400, "E_REQ_INVALID", "This request is outside the mock Ops API.");
  };
}

function cursorFor(id: string) {
  return `registry after:${id}`;
}
