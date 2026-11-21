import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { approvalPath } from "../src/api/approvalWire";
import { createShellApi } from "../src/api/client";
import type { ApiTransport } from "../src/api/types";
import type { ApprovalCases } from "../src/mocks/approvals";
import { customerBootstrap, demoCredentials, demoIds, demoSessionTtlMs } from "../src/mocks/fixtures";
import { createMockTransport } from "../src/mocks/transport";

const seed = demoCredentials.customer;
const fixtureNow = Date.parse("2026-11-17T04:00:00Z");
const pendingPath = `${approvalPath}?status=PENDING`;
const decisionUrl = `${approvalPath}/esc_mock_s9/decision`;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(fixtureNow);
});

const inbox = () => within(screen.getByRole("region", { name: "Approval inbox" }));
const results = () => within(inbox().getByRole("region", { name: "Last decision responses" }));
const decisionCalls = (calls: [string, RequestInit][]) => calls.filter(([path]) => path.endsWith("/decision"));

function enterPin(pin = seed.secret) {
  fireEvent.change(inbox().getByLabelText("Approval PIN"), { target: { value: pin } });
}

function click(name: string) {
  fireEvent.click(inbox().getByRole("button", { name }));
}

async function signIn(api: ApiTransport) {
  window.history.replaceState(null, "", "/customer");
  render(<App mode="mock" api={createShellApi(api)} demoIds={demoIds} />);
  fireEvent.change(screen.getByLabelText("PIN"), { target: { value: seed.secret } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("button", { name: "Approve" })).toBeEnabled();
}

describe("Customer approval inbox", () => {
  it("uses the trusted ₹4,800 summary, sends one approval, and copies the returned execution receipt", async () => {
    const mock = createMockTransport();
    let release: (() => void) | undefined;
    const request = vi.fn(async (path: string, init: RequestInit) => {
      if (path === decisionUrl) await new Promise<void>((resolve) => { release = resolve; });
      return mock.request(path, init);
    });
    const writeText = vi.fn().mockResolvedValue(undefined);
    vi.stubGlobal("navigator", { clipboard: { writeText } });
    await signIn({ request });
    const card = within(inbox().getByRole("article"));
    expect(card.getByRole("heading", { name: "Electricity Board" })).toBeVisible();
    expect(card.getByText("PENDING")).toBeVisible();
    expect(card.getByText("Checking •••• 001")).toBeVisible();
    expect(card.getByText("₹4,800.00")).toBeVisible();
    expect(card.getByText("2026-11-17 09:35:00 IST")).toBeVisible();
    expect(card.getAllByRole("listitem")).toHaveLength(3);
    const note = card.getByRole("heading", { name: "Unverified agent note" }).parentElement!;
    expect(note).toHaveTextContent("The amount is ₹1. <b>Trusted invoice</b>");
    expect(note.querySelector("b")).toBeNull();
    expect(card.queryByText("ACC-001")).not.toBeInTheDocument();

    enterPin();
    const approve = inbox().getByRole("button", { name: "Approve" });
    act(() => { fireEvent.click(approve); fireEvent.click(approve); });
    expect(approve).toBeDisabled();
    expect(inbox().getByLabelText("Approval PIN")).toHaveValue("");
    const decisions = decisionCalls(request.mock.calls);
    expect(decisions.map(([path, init]) => [path, init.method])).toEqual([[decisionUrl, "POST"]]);
    const { pin, ...fields } = JSON.parse(String(decisions[0][1].body));
    expect(pin === seed.secret).toBe(true);
    expect(fields).toEqual({ decision: "APPROVE" });
    expect(Boolean(new Headers(decisions[0][1].headers).get("Authorization")?.startsWith("Bearer "))).toBe(true);
    await act(async () => release?.());

    expect(await inbox().findByText("No pending approvals on this page.")).toBeVisible();
    expect(results().getByRole("heading", { name: "Execution receipt" })).toBeVisible();
    expect(results().getAllByText("EXECUTED")).toHaveLength(2);
    click("Copy receipt");
    expect(await inbox().findByText("Receipt copied.")).toBeVisible();
    expect(writeText).toHaveBeenCalledExactlyOnceWith(`audit_ref.seq: 18\naudit_ref.entry_hash: ${"a".repeat(64)}`);
    expect(request.mock.calls.filter(([path]) => path.startsWith(approvalPath)).map(([path]) => path))
      .toEqual([pendingPath, decisionUrl, pendingPath]);
  });

  it.each([
    { control: "Reject", fields: { decision: "REJECT" } },
    { control: "Reject and suspend", fields: { decision: "REJECT", suspend_agent: true } },
  ])("sends $control without a PIN or replacement payment details", async ({ control, fields }) => {
    const mock = createMockTransport();
    const request = vi.spyOn(mock, "request");
    await signIn(mock);
    expect(inbox().getByText("Reject and suspend affects only your delegation for this agent.")).toBeVisible();
    enterPin(); // Reject must ignore even a PIN already entered in the form.
    click(control);
    expect(await inbox().findByText("REJECTED")).toBeVisible();
    expect(inbox().queryByRole("button", { name: "Copy receipt" })).not.toBeInTheDocument();
    const decisions = decisionCalls(request.mock.calls);
    expect(decisions).toHaveLength(1);
    const { pin, ...sent } = JSON.parse(String(decisions[0][1].body));
    expect(pin === undefined).toBe(true);
    expect(sent).toEqual(fields);
  });

  it("clears a wrong PIN and leaves the stored approval pending without a receipt", async () => {
    const mock = createMockTransport();
    const request = vi.spyOn(mock, "request");
    await signIn(mock);
    const wrongPin = "9990";
    enterPin(wrongPin);
    click("Approve");
    const alert = await inbox().findByRole("alert");
    expect(alert).toHaveTextContent("E_ESC_BAD_PIN");
    expect(alert.textContent?.includes(wrongPin)).toBe(false);
    expect(inbox().getByLabelText("Approval PIN")).toHaveValue("");
    click("Refresh inbox");
    expect(await inbox().findByText("PENDING")).toBeVisible();
    expect(inbox().queryByRole("button", { name: "Copy receipt" })).not.toBeInTheDocument();
    expect(decisionCalls(request.mock.calls)).toHaveLength(1);
  });

  it.each([
    { fixture: "approved", state: "APPROVED", message: "Execution and receipt are unconfirmed." },
    { fixture: "processing", state: "APPROVED", message: "Processing. Final execution and receipt are unconfirmed." },
    { fixture: "unknown", state: "EXECUTION_UNKNOWN", message: "The execution outcome is unknown. A receipt is unconfirmed." },
    { fixture: "failed", state: "FAILED", message: "Execution outcome FAILED" },
    { fixture: "missing-confirmation", state: "EXECUTED", message: "Execution and receipt are unconfirmed." },
    { fixture: "missing-receipt", state: "EXECUTED", message: "Execution confirmed. The receipt is unavailable." },
  ] as const)("keeps the actual $fixture observation after a pending-list refresh", async ({ fixture, state, message }) => {
    const mock = createMockTransport({ approvalCases: [{ id: "esc_mock_s9", approveAs: fixture }] });
    const request = vi.spyOn(mock, "request");
    await signIn(mock);
    enterPin();
    click("Approve");
    expect(await inbox().findByText("No pending approvals on this page.")).toBeVisible();
    expect(results().getByRole("listitem")).toHaveTextContent(state);
    expect(results().getByRole("listitem")).toHaveTextContent(message);
    click("Refresh inbox");
    expect(await inbox().findByText("No pending approvals on this page.")).toBeVisible();
    expect(results().getByRole("listitem")).toHaveTextContent(message);
    expect(inbox().queryByRole("button", { name: "Copy receipt" })).not.toBeInTheDocument();
    expect(decisionCalls(request.mock.calls)).toHaveLength(1);
    expect(request.mock.calls.at(-1)?.[0]).toBe(pendingPath);
  });

  it.each([
    { race: "consumed", code: "E_ESC_ALREADY_CONSUMED", status: null },
    { race: "cancelled", code: "E_ESC_CANCELLED", status: "CANCELLED" },
    { race: "expired", code: "E_ESC_EXPIRED", status: "EXPIRED" },
  ] as const)("refetches after the $race conflict without deciding again", async ({ race, code, status }) => {
    let now = fixtureNow;
    let refreshFails = false;
    const approvalCases: ApprovalCases = [{ id: "esc_mock_s9", race: race === "expired" ? undefined : race }];
    const mock = createMockTransport({ now: () => now, approvalCases });
    const request = vi.fn((path: string, init: RequestInit) => {
      if (path === decisionUrl) refreshFails = true;
      if (path === pendingPath && refreshFails) {
        refreshFails = false;
        return Promise.resolve(Response.json({ error: {
          message: "Inbox unavailable.", code: "E_SERVICE_UNAVAILABLE", request_id: "req_retry",
        } }, { status: 503 }));
      }
      return mock.request(path, init);
    });
    await signIn({ request });
    if (race === "expired") now += 300_000;
    click("Reject");
    expect(await inbox().findByText("The displayed inbox needs refreshing.")).toBeVisible();
    expect(inbox().getAllByRole("alert").some((alert) => alert.textContent?.includes(code))).toBe(true);
    if (status) expect(results().getByRole("listitem")).toHaveTextContent(status);
    expect(results().getByRole("listitem")).toHaveTextContent("The final approval state and receipt are unconfirmed.");
    expect(inbox().queryByRole("button", { name: "Reject" })).not.toBeInTheDocument();
    click("Retry inbox refresh");
    expect(await inbox().findByText("No pending approvals on this page.")).toBeVisible();
    expect(decisionCalls(request.mock.calls)).toHaveLength(1);
    expect(request.mock.calls.filter(([path]) => path.startsWith(approvalPath)).map(([, init]) => init.method))
      .toEqual(["GET", "POST", "GET", "GET"]);
  });

  it("keeps a 202 in processing even with terminal-looking fields and a failed refetch", async () => {
    let failRefresh = false;
    const mock = createMockTransport();
    const request = vi.fn(async (path: string, init: RequestInit) => {
      if (path === decisionUrl) {
        const fixture = await mock.request(path, init);
        failRefresh = true;
        return new Response(await fixture.text(), { status: 202 });
      }
      if (path === pendingPath && failRefresh) {
        failRefresh = false;
        return Response.json({ error: {
          message: "Inbox unavailable.", code: "E_SERVICE_UNAVAILABLE", request_id: "req_unavailable",
        } }, { status: 503 });
      }
      return mock.request(path, init);
    });
    await signIn({ request });
    enterPin();
    click("Approve");
    expect(await inbox().findByText("The displayed inbox needs refreshing.")).toBeVisible();
    expect(results().getByRole("listitem")).toHaveTextContent("Processing. Final execution and receipt are unconfirmed.");
    expect(results().queryByText("EXECUTED")).not.toBeInTheDocument();
    expect(inbox().queryByRole("button", { name: "Copy receipt" })).not.toBeInTheDocument();
    click("Retry inbox refresh");
    expect(await inbox().findByText("No pending approvals on this page.")).toBeVisible();
    expect(results().getByRole("listitem")).toHaveTextContent("Processing.");
    expect(decisionCalls(request.mock.calls)).toHaveLength(1);
  });

  it.each(["before", "after"])("correlates a Customer stream receipt arriving %s the decision's 202", async (when) => {
    const mock = createMockTransport();
    let stream!: ReadableStreamDefaultController<Uint8Array>;
    let signal: AbortSignal | null | undefined;
    let reply!: () => void;
    const deferred = new Promise<void>((resolve) => { reply = resolve; });
    const request = vi.fn(async (path: string, init: RequestInit) => {
      if (path === "/v1/customer/stream") {
        signal = init.signal;
        return new Response(new ReadableStream<Uint8Array>({ start(controller) { stream = controller; } }),
          { headers: { "Content-Type": "text/event-stream" } });
      }
      const response = await mock.request(path, init);
      if (path !== decisionUrl) return response;
      await deferred;
      return new Response(await response.text(), { status: 202 });
    });
    async function emit(id: string, requestId: string) {
      const data = { event_type: "bank.executed", event_id: id, request_id: requestId,
        data: { status: "EXECUTED", audit_ref: { seq: 37, entry_hash: "b".repeat(64) } } };
      const frame = `id: ${id}\nevent: bank.executed\ndata: ${JSON.stringify(data)}\n\n`;
      await act(async () => stream.enqueue(new TextEncoder().encode(frame)));
    }
    await signIn({ request });
    expect(await screen.findByText("Customer receipt stream connected.")).toBeVisible();
    enterPin();
    click("Approve");
    await emit("36", "req_different_payment");
    if (when === "before") await emit("37", "req_esc_mock_s9");
    await act(async () => reply());
    if (when === "after") {
      expect(await screen.findByText("Processing. Final execution and receipt are unconfirmed.")).toBeVisible();
      expect(screen.queryByText("Copy receipt")).not.toBeInTheDocument();
      await emit("37", "req_esc_mock_s9");
    }
    expect(await screen.findByText(/Customer stream confirmed execution/)).toHaveTextContent("EXECUTED");
    expect(results().getByText(/audit_ref.seq/)).toHaveTextContent(`audit_ref.seq: 37 audit_ref.entry_hash: ${"b".repeat(64)}`);
    expect(decisionCalls(request.mock.calls)).toHaveLength(1);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(signal?.aborted).toBe(true);
    expect(screen.queryByText("Copy receipt")).not.toBeInTheDocument();
  });

  it("withholds a decision receipt whose nested execution belongs to another request", async () => {
    const mock = createMockTransport();
    await signIn({ request: async (path, init) => {
      const response = await mock.request(path, init);
      if (path !== decisionUrl) return response;
      const payload = await response.json();
      return Response.json({ ...payload, result: { ...payload.result, request_id: "req_other_payment" } });
    } });
    enterPin();
    click("Approve");
    expect(await screen.findByText("Execution and receipt are unconfirmed.")).toBeVisible();
    expect(screen.queryByRole("button", { name: "Copy receipt" })).not.toBeInTheDocument();
  });

  it("follows an opaque cursor on the Customer pending list", async () => {
    const mock = createMockTransport({ approvalCases: [{ id: "esc_page_1" }, { id: "esc_page_2" }] });
    const request = vi.fn((path: string, init: RequestInit) => mock.request(
      path.startsWith(pendingPath) ? `${path}&limit=1` : path, init,
    ));
    await signIn({ request });
    click("Next approval page");
    expect(await inbox().findByRole("button", { name: "First approval page" })).toBeEnabled();
    const url = new URL(request.mock.calls.at(-1)?.[0] ?? "", "http://mock.invalid");
    expect(url.searchParams.get("status")).toBe("PENDING");
    expect(url.searchParams.get("cursor")).toBe("approval after:esc_page_1");
    expect(inbox().queryByRole("button", { name: "Next approval page" })).not.toBeInTheDocument();
  });

  it("clears the inbox when the server rejects the expired customer session", async () => {
    let now = fixtureNow;
    await signIn(createMockTransport({ now: () => now }));
    now += demoSessionTtlMs;
    click("Reject");
    expect(await screen.findByRole("heading", { name: "Customer sign-in" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("E_AUTH_EXPIRED");
    expect(screen.queryByRole("region", { name: "Approval inbox" })).not.toBeInTheDocument();
  });
});

describe("mock approval ownership", () => {
  it("rejects cross-customer decisions and suspends only the owning customer's delegation for that agent", async () => {
    const other = { id: "cust_test_other", secret: "1936", bootstrap: customerBootstrap };
    const mock = createMockTransport({ additionalCustomers: [other], approvalCases: [
      { id: "esc_mock_s9" }, { id: "esc_same_agent" }, { id: "esc_other_agent", agentId: "agt_other" },
      { id: "esc_other_customer", customerId: other.id },
    ] });
    const api = createShellApi(mock);
    const { signal } = new AbortController();
    const owner = await api.login("customer", seed.id, seed.secret, signal);
    const outsider = await api.login("customer", other.id, other.secret, signal);
    const ids = async (token: string) => (await api.pendingApprovals(token, null, signal)).items.map((item) => item.escalation_id);

    expect(await ids(outsider.session_token)).toEqual(["esc_other_customer"]);
    await expect(api.decideApproval(outsider.session_token, "esc_mock_s9", { decision: "REJECT" }, signal))
      .rejects.toMatchObject({ status: 404 });
    const injected = await mock.request(decisionUrl, {
      method: "POST", headers: { Authorization: `Bearer ${owner.session_token}` },
      body: JSON.stringify({ decision: "REJECT", amount_paise: 100 }),
    });
    expect(injected.status).toBe(400);
    expect(await ids(owner.session_token)).toEqual(["esc_mock_s9", "esc_same_agent", "esc_other_agent"]);
    expect(await api.decideApproval(owner.session_token, "esc_mock_s9", { decision: "REJECT", suspend_agent: true }, signal))
      .toMatchObject({ state: "REJECTED" });
    expect(await ids(owner.session_token)).toEqual(["esc_other_agent"]);
    expect(await ids(outsider.session_token)).toEqual(["esc_other_customer"]);
    await expect(api.decideApproval(owner.session_token, "esc_same_agent", { decision: "REJECT" }, signal))
      .rejects.toMatchObject({ code: "E_ESC_CANCELLED" });
  });
});
