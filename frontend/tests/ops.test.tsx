import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { createShellApi } from "../src/api/client";
import { createGatewayTransport } from "../src/api/gatewayTransport";
import { opsStreamPath } from "../src/api/opsWire";
import { shellRoutes } from "../src/api/shellWire";
import type { ApiTransport } from "../src/api/types";
import { demoCredentials, demoIds, demoSessionTtlMs, registeredAgents } from "../src/mocks/fixtures";
import { createMockTransport } from "../src/mocks/transport";

const now = Date.parse("2026-11-17T04:00:00Z");
const agentsPath = shellRoutes.ops.read;
const suspendPath = `${agentsPath}/agt_billpal/suspend`;
const reinstatePath = `${agentsPath}/agt_billpal/reinstate`;
const seed = demoCredentials.ops;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(now);
});

const registry = () => within(screen.getByRole("region", { name: "Registered agents" }));
const feed = () => within(screen.getByRole("region", { name: "Live decision feed" }));
const billPalRow = () => within(registry().getByText("BillPal").closest("li")!);

function visitOps(api: ApiTransport, mode: "mock" | "real" = "mock") {
  window.history.replaceState(null, "", "/ops");
  const client = createShellApi(api);
  return render(mode === "real" ? <App mode="real" api={client} /> : <App mode="mock" api={client} demoIds={demoIds} />);
}

function submitOpsLogin() {
  fireEvent.change(screen.getByLabelText("Operator ID"), { target: { value: seed.id } });
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: seed.secret } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

function clickAgentControl(name: string) {
  fireEvent.click(registry().getByRole("button", { name }));
}

function sseResponse() {
  const encoder = new TextEncoder();
  let writer: ReadableStreamDefaultController<Uint8Array>;
  const cancelled = vi.fn();
  const body = new ReadableStream<Uint8Array>({
    start(controller) { writer = controller; },
    cancel: cancelled,
  });
  return {
    response: new Response(body, { headers: { "Content-Type": "text/event-stream; charset=utf-8" } }),
    body, cancelled, push: (chunk: string | Uint8Array) => writer.enqueue(typeof chunk === "string" ? encoder.encode(chunk) : chunk),
    close: () => writer.close(),
  };
}

function decisionFrame(id: string, request_id: string, data: object) {
  const event_type = "action.decision";
  const envelope = { event_id: id, event_type, request_id, agent_id: "agt_test",
    occurred_at: new Date(now).toISOString(), data };
  return `id: ${id}\r\nevent: ${event_type}\r\n${JSON.stringify(envelope, null, 2)
    .split("\n").map((line) => `data: ${line}`).join("\r\n")}\r\n\r\n`;
}

function fakeTimers() {
  vi.useRealTimers();
  vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout", "setInterval", "clearInterval"] });
  vi.setSystemTime(now);
}

describe("Ops decision stream", () => {
  it("authenticates fetch, parses fragments, deduplicates replay, polls and refetches after reconnect and resync", async () => {
    fakeTimers();
    const mock = createMockTransport();
    const first = sseResponse();
    const reconnected = sseResponse();
    const gateway = "http://127.0.0.1:8000";
    let connections = 0;
    let suspended = false;
    const fetch = vi.fn(async (input: string, init: RequestInit) => {
      const path = new URL(input).pathname;
      if (path === opsStreamPath) {
        connections += 1;
        if (connections === 1) return first.response;
        if (connections === 3) return reconnected.response;
        return Response.json({ error: {
          message: "Stream unavailable.", code: "E_SERVICE_UNAVAILABLE", request_id: "req_stream_unavailable",
        } }, { status: 503 });
      }
      if (path === agentsPath && suspended) return Response.json({
        ...registeredAgents, items: registeredAgents.items.map((row) => ({ ...row, status: "SUSPENDED" })),
      });
      return mock.request(path, init);
    });
    vi.stubGlobal("fetch", fetch);
    visitOps(createGatewayTransport(gateway), "real");
    await act(async () => submitOpsLogin());
    expect(feed().getByText("Live stream connected.")).toBeVisible();
    expect(billPalRow().getByText("ACTIVE")).toBeVisible();

    const replayed = decisionFrame("event-18", "req_fragmented", {
      decision: "ESCALATE", risk: { score: 0, signals: [{ message: "Risk model · checked" }] },
    });
    const bytes = new TextEncoder().encode(replayed);
    await act(async () => {
      for (let offset = 0; offset < bytes.length; offset += 1) first.push(bytes.slice(offset, offset + 1));
      first.push(replayed);
    });
    expect(feed().getAllByText("req_fragmented")).toHaveLength(1);
    expect(feed().getByText("Risk model · checked")).toBeVisible();
    expect(feed().getByText("0")).toBeVisible();
    expect(feed().getByText("2026-11-17 09:30:00 IST")).toBeVisible();

    const listCalls = () => fetch.mock.calls.filter(([url]) => new URL(url).pathname === agentsPath);
    await act(async () => first.close());
    expect(feed().getByText(/Reconnecting and polling agent status every two seconds/)).toBeVisible();
    const beforePoll = listCalls().length;
    suspended = true;
    await act(async () => { await vi.advanceTimersByTimeAsync(1_999); });
    expect(listCalls()).toHaveLength(beforePoll);
    await act(async () => { await vi.advanceTimersByTimeAsync(1); });
    expect(listCalls()).toHaveLength(beforePoll + 1);
    expect(billPalRow().getByText("SUSPENDED")).toBeVisible();
    expect(feed().getByRole("alert")).toHaveTextContent("req_stream_unavailable");
    await act(async () => { await vi.advanceTimersByTimeAsync(2_000); });
    expect(feed().getByText("Live stream connected.")).toBeVisible();
    expect(listCalls().length).toBeGreaterThan(beforePoll + 1);
    const streams = fetch.mock.calls.filter(([url]) => new URL(url).pathname === opsStreamPath);
    expect(streams.map(([url]) => url)).toEqual(Array(3).fill(`${gateway}${opsStreamPath}`));
    expect(streams.map(([, init]) => new Headers(init.headers).get("Last-Event-ID")))
      .toEqual([null, "event-18", "event-18"]);
    expect(streams.every(([, init]) => new Headers(init.headers).get("Authorization")?.startsWith("Bearer ")
      && init.credentials === "omit")).toBe(true);
    expect(window.location.search).toBe("");

    const beforeResync = listCalls().length;
    await act(async () => {
      reconnected.push("event: resync\ndata: {}\n\n");
      reconnected.push(replayed);
      reconnected.push(decisionFrame("event-19", "req_after_resync", { decision: "DENY", reason_codes: ["E_RISK_HIGH"] }));
    });
    expect(listCalls()).toHaveLength(beforeResync + 1);
    expect(feed().getByText(/Decision history may be incomplete/)).toBeVisible();
    expect(feed().getAllByText("req_fragmented")).toHaveLength(1);
    const denied = within(feed().getByText("req_after_resync").closest("li")!);
    expect(denied.getByText("E_RISK_HIGH")).toBeVisible();
    expect(denied.getByText("Unavailable")).toBeVisible();
    expect(denied.queryByText("0")).not.toBeInTheDocument();
    expect(denied.getByText("agt_test")).toBeVisible();

    await act(async () => fireEvent.click(screen.getByRole("button", { name: "Sign out" })));
    expect(streams.at(-1)?.[1].signal?.aborted).toBe(true);
    expect(reconnected.cancelled).toHaveBeenCalledOnce();
    expect(screen.queryByRole("region", { name: "Live decision feed" })).not.toBeInTheDocument();
    const afterLogout = fetch.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000); });
    expect(fetch.mock.calls).toHaveLength(afterLogout);
  });

  it("stops both recovery timers when the Ops session expires", async () => {
    fakeTimers();
    const mock = createMockTransport();
    const stream = sseResponse();
    const request = vi.fn((path: string, init: RequestInit) =>
      path === opsStreamPath ? Promise.resolve(stream.response) : mock.request(path, init));
    visitOps({ request });
    await act(async () => submitOpsLogin());
    await act(async () => { await vi.advanceTimersByTimeAsync(demoSessionTtlMs - 500); });
    await act(async () => stream.close());
    expect(feed().getByText(/Reconnecting and polling/)).toBeVisible();
    await act(async () => { await vi.advanceTimersByTimeAsync(500); });
    expect(screen.getByRole("heading", { name: "Ops sign-in" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("UI_SESSION_EXPIRED");
    const afterExpiry = request.mock.calls.length;
    await act(async () => { await vi.advanceTimersByTimeAsync(6_000); });
    expect(request.mock.calls).toHaveLength(afterExpiry);
  });

  it("aborts an in-flight registry refresh on logout and discards the late response", async () => {
    const mock = createMockTransport();
    let finish: (() => void) | undefined;
    let readCount = 0;
    const request = vi.fn(async (path: string, init: RequestInit) => {
      const response = await mock.request(path, init);
      if (path === agentsPath && ++readCount === 2) await new Promise<void>((resolve) => { finish = resolve; });
      return response;
    });
    visitOps({ request });
    submitOpsLogin();
    await screen.findByRole("button", { name: "Suspend globally BillPal" });
    await act(async () => clickAgentControl("Refresh agents"));
    expect(finish).toBeTypeOf("function");
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    const reads = request.mock.calls.filter(([path]) => path === agentsPath);
    expect(reads[1][1].signal?.aborted).toBe(true);
    await act(async () => finish?.());
    expect(screen.getByRole("heading", { name: "Ops sign-in" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "Registered agents" })).not.toBeInTheDocument();
  });
});

describe("Ops registry against stateful fixtures", () => {
  it("suspends globally once on a duplicate click and reinstates for future actions", async () => {
    const mock = createMockTransport();
    let finishSuspend: (() => void) | undefined;
    const request = vi.fn(async (path: string, init: RequestInit) => {
      if (path === suspendPath) await new Promise<void>((resolve) => { finishSuspend = resolve; });
      return mock.request(path, init);
    });
    visitOps({ request });
    submitOpsLogin();
    const suspend = await screen.findByRole("button", { name: "Suspend globally BillPal" });
    expect(await feed().findByText("req_ops_demo_review")).toBeVisible();
    expect(registry().getByText(/Suspension applies to this agent across all customers/)).toBeVisible();
    act(() => { fireEvent.click(suspend); fireEvent.click(suspend); });
    expect(suspend).toBeDisabled();
    expect(request.mock.calls.filter(([path]) => path === suspendPath)).toHaveLength(1);
    await act(async () => finishSuspend?.());
    expect(billPalRow().getByText("SUSPENDED")).toBeVisible();
    clickAgentControl("Reinstate BillPal");
    expect(await registry().findByText("Reinstatement acknowledged for BillPal for future actions.")).toBeVisible();
    expect(await billPalRow().findByText("ACTIVE")).toBeVisible();
    const posts = request.mock.calls.filter(([, init]) => init.method === "POST").slice(1);
    expect(posts.map(([path]) => path)).toEqual([suspendPath, reinstatePath]);
    expect(posts.every(([, init]) => init.body === undefined)).toBe(true);
  });

  it("shows a failed mutation and preserves the current provider status", async () => {
    const mock = createMockTransport();
    const request = vi.fn((path: string, init: RequestInit) => path === suspendPath
      ? Promise.resolve(Response.json({ error: {
        code: "E_REQ_INVALID", message: "Agent change rejected.", request_id: "req_change_rejected",
      } }, { status: 403 }))
      : mock.request(path, init));
    visitOps({ request });
    submitOpsLogin();
    await screen.findByRole("button", { name: "Suspend globally BillPal" });
    clickAgentControl("Suspend globally BillPal");
    const alert = await registry().findByRole("alert");
    expect(alert).toHaveTextContent("E_REQ_INVALID");
    expect(alert).toHaveTextContent("req_change_rejected");
    expect(billPalRow().getByText("ACTIVE")).toBeVisible();
    expect(request.mock.calls.filter(([path]) => path === suspendPath)).toHaveLength(1);
  });

  it("reads current status after an uncertain mutation without replaying the POST", async () => {
    const mock = createMockTransport();
    const request = vi.fn(async (path: string, init: RequestInit) => {
      const response = await mock.request(path, init);
      if (path === suspendPath && response.ok) return Response.json({
        error: { code: "E_SERVICE_UNAVAILABLE", message: "Acknowledgement lost.", request_id: "req_uncertain" },
      }, { status: 503 });
      return response;
    });
    visitOps({ request });
    submitOpsLogin();
    await screen.findByRole("button", { name: "Suspend globally BillPal" });
    clickAgentControl("Suspend globally BillPal");
    expect(await registry().findByText("Acknowledgement unavailable for BillPal.")).toBeVisible();
    expect(await billPalRow().findByText("SUSPENDED")).toBeVisible();
    expect(registry().getByRole("alert")).toHaveTextContent("req_uncertain");
    const mutation = request.mock.calls.findIndex(([path]) => path === suspendPath);
    const after = request.mock.calls.slice(mutation + 1);
    expect(after.length).toBeGreaterThan(0);
    expect(after.every(([path, init]) => path === agentsPath && init.method === "GET")).toBe(true);
  });

  it("keeps suspension acknowledged and stale after a failed refresh despite a late ACTIVE read", async () => {
    const mock = createMockTransport();
    const stream = sseResponse();
    let finishOldRead: (() => void) | undefined;
    let reads = 0;
    const request = vi.fn(async (path: string, init: RequestInit) => {
      if (path === opsStreamPath) return stream.response;
      if (path === agentsPath) {
        reads += 1;
        if (reads === 2) {
          const old = await mock.request(path, init);
          await new Promise<void>((resolve) => { finishOldRead = resolve; });
          return old;
        }
        if (reads === 3) return Response.json({ error: {
          code: "E_SERVICE_UNAVAILABLE", message: "Agent list unavailable.", request_id: "req_refresh_failed",
        } }, { status: 503 });
      }
      return mock.request(path, init);
    });
    visitOps({ request });
    submitOpsLogin();
    await screen.findByRole("button", { name: "Suspend globally BillPal" });
    await act(async () => clickAgentControl("Refresh agents"));
    expect(finishOldRead).toBeTypeOf("function");
    clickAgentControl("Suspend globally BillPal");
    expect(await registry().findByText("Global suspension acknowledged for BillPal.")).toBeVisible();
    expect(await registry().findByRole("alert")).toHaveTextContent("req_refresh_failed");
    const listReads = request.mock.calls.filter(([path]) => path === agentsPath);
    expect(listReads[1][1].signal?.aborted).toBe(true);
    await act(async () => finishOldRead?.());
    expect(billPalRow().getByText("Stale status · Last listed ACTIVE")).toBeVisible();
    expect(billPalRow().queryByText("ACTIVE", { exact: true })).not.toBeInTheDocument();
    expect(billPalRow().getByRole("button", { name: "Suspend globally BillPal" })).toBeDisabled();

    clickAgentControl("Retry agent refresh");
    expect(await billPalRow().findByText("SUSPENDED")).toBeVisible();
    expect(request.mock.calls.at(-1)?.[1].method).toBe("GET");
    expect(request.mock.calls.filter(([path]) => path === suspendPath)).toHaveLength(1);
  });

  it("follows the opaque agent cursor", async () => {
    const mock = createMockTransport();
    const request = vi.fn((path: string, init: RequestInit) => mock.request(path.startsWith(agentsPath)
      ? path + (path.includes("?") ? "&" : "?") + "limit=1" : path, init));
    visitOps({ request });
    submitOpsLogin();
    expect(await screen.findByRole("button", { name: "Next agent page" })).toBeEnabled();
    clickAgentControl("Next agent page");
    expect(await registry().findByText("Budget Assistant")).toBeVisible();
    const { searchParams } = new URL(request.mock.calls.at(-1)?.[0] ?? "", "http://mock.invalid");
    expect(searchParams.get("cursor")).toBe("registry after:agt_billpal");
    expect(registry().queryByText("BillPal")).not.toBeInTheDocument();
  });

  it("keeps Ops UI and transport reads and controls outside the Customer session", async () => {
    const mock = createMockTransport();
    const request = vi.spyOn(mock, "request");
    const api = createShellApi(mock);
    const { id, secret } = demoCredentials.customer;
    const { signal } = new AbortController();
    const customer = await api.login("customer", id, secret, signal);
    const bearer = { Authorization: `Bearer ${customer.session_token}` };
    for (const [path, method] of [[opsStreamPath, "GET"], [agentsPath, "GET"], [suspendPath, "POST"], [reinstatePath, "POST"]]) {
      expect((await mock.request(path, { method, headers: bearer })).status).toBe(401);
    }
    request.mockClear();
    window.history.replaceState(null, "", "/customer");
    render(<App mode="mock" api={api} demoIds={demoIds} />);
    fireEvent.change(screen.getByLabelText("PIN"), { target: { value: secret } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByRole("heading", { name: "Your mandates" })).toBeVisible();
    expect(screen.queryByRole("region", { name: "Live decision feed" })).not.toBeInTheDocument();
    expect(screen.queryByRole("button", { name: /Suspend globally/ })).not.toBeInTheDocument();
    expect(request.mock.calls.some(([path]) => path.startsWith("/v1/ops/"))).toBe(false);
  });
});
