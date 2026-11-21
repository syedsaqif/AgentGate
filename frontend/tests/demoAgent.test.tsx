import { fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { afterAll, beforeAll, beforeEach, expect, it, vi } from "vitest";
import DemoAgentPage from "../src/pages/demo/agent/DemoAgentPage";
import type { Scenario } from "../src/pages/demo/agent/agentApi";

const normal: Scenario = {
  id: "P1", group: "normal", kind: "normal", title: "Routine bill", description: "Pay a bill",
  prompt: "Pay my electricity bill of ₹1,850.", attachment: null, prerequisites: "Active mandate",
  expected_stop_point: "Bank execution", expected_verdict: "ALLOW", observation_available: true, source: "live_supported",
};
const retry: Scenario = { ...normal, id: "P6", title: "Retry original", prompt: "Retry the original bill." };
const fixture: Scenario = { ...normal, id: "C1", kind: "attack", group: "identity_integrity", title: "Proof mismatch",
  source: "defensive_test", fixture: { stage: "identity", reason_code: "E_AUTH_KEY_MISMATCH" }, expected_verdict: "DENY" };
const frame = (seq: number, step: string, metadata = {}) =>
  `id: ${seq}\nevent: agent_step\ndata: ${JSON.stringify({ step, ...metadata })}\n\n`;
const requestId = "req_0123456789abcdef";

const dialog = HTMLDialogElement.prototype;
const replacements = {
  showModal(this: HTMLDialogElement) { this.open = true; },
  close(this: HTMLDialogElement) { this.open = false; this.dispatchEvent(new Event("close")); },
};
const restore: (() => void)[] = [];
beforeAll(() => {
  for (const [key, value] of Object.entries(replacements)) {
    if (typeof Reflect.get(dialog, key) === "function") continue;
    Object.defineProperty(dialog, key, { configurable: true, value });
    restore.push(() => { Reflect.deleteProperty(dialog, key); });
  }
});
afterAll(() => restore.forEach((callback) => callback()));
beforeEach(() => {
  sessionStorage.clear();
  window.history.replaceState(null, "", "/demo/agent");
});

function mockBackend(stream: (id: string, last: string | null) => string) {
  const posts: Record<string, unknown>[] = [];
  const gets: { id: string; last: string | null }[] = [];
  vi.stubGlobal("fetch", vi.fn(async (input: string, init: RequestInit) => {
    const url = new URL(input);
    if (url.pathname.endsWith("/scenarios")) return Response.json({
      items: [normal, retry, fixture], policy_inputs: { payees: [], categories: [] },
    });
    if (url.pathname.endsWith("/agent/run")) {
      posts.push(JSON.parse(String(init.body)));
      return Response.json({ run_id: `run_${posts.length}` }, { status: 202 });
    }
    const id = url.searchParams.get("run_id")!;
    const last = new Headers(init.headers).get("Last-Event-ID");
    gets.push({ id, last });
    return new Response(stream(id, last), { headers: { "Content-Type": "text/event-stream" } });
  }));
  return { posts, gets };
}

const click = (name: string) => fireEvent.click(screen.getByRole("button", { name }));
it("can start a new chat from an invalid run link", async () => {
  const api = mockBackend(() => "");
  window.history.replaceState(null, "", "/demo/agent?run_id=invalid");
  render(<DemoAgentPage />);
  expect(await screen.findByText(/This run link is invalid/)).toBeVisible();
  click("New chat");
  expect(screen.getByRole("heading", { name: "What would you like to pay?" })).toBeVisible();
  expect(window.location.search).toBe("");
  expect(api.gets).toHaveLength(0);
});

async function lab() {
  await waitFor(() => expect(screen.getByRole("button", { name: "Test lab" })).toBeEnabled());
  click("Test lab");
  return within(screen.getByRole("dialog"));
}

it("uses an edited prompt without dispatching the original preset, and shows clarification", async () => {
  const api = mockBackend(() => frame(1, "clarification", { message: "Which payee?" }) + frame(2, "completed", { verdict: "CLARIFY" }));
  render(<DemoAgentPage />);
  const sheet = await lab();
  fireEvent.click(sheet.getAllByRole("button", { name: "Use prompt" })[0]);
  const composer = screen.getByLabelText("Message");
  expect(composer).toHaveValue(normal.prompt);
  expect(api.posts).toHaveLength(0);
  fireEvent.change(composer, { target: { value: "Pay my landlord ₹20,000." } });
  click("Send");
  expect(await screen.findByText("Clarification before dispatch")).toBeVisible();
  expect(api.posts).toEqual([{ prompt: "Pay my landlord ₹20,000.", agent_mode: "naive" }]);
  expect(screen.queryByRole("link", { name: /View Gate run/ })).not.toBeInTheDocument();
});

it("runs the catalogue selection, resumes GET with its cursor, and shows a mismatch", async () => {
  const api = mockBackend((id, last) => {
    if (id === "run_1" && !last) return frame(1, "tool_call", { tool: "pay_bill", args: { amount_paise: 185000, pin: "secret" } });
    return frame(2, "tool_result", { request_id: requestId, verdict: "DENY", reason_code: "E_POLICY_PAYEE_NOT_ALLOWED" })
      + frame(3, "completed", { verdict: "DENY" });
  });
  render(<DemoAgentPage />);
  fireEvent.click((await lab()).getAllByRole("button", { name: "Run" })[0]);
  expect(await screen.findByText("Expected vs actual · differs")).toBeVisible();
  expect(screen.queryByText(/secret/)).not.toBeInTheDocument();
  expect(api.gets).toEqual([{ id: "run_1", last: null }, { id: "run_1", last: "1" }]);
  expect(api.posts).toEqual([{ scenario_id: "P1", agent_mode: "naive" }]);
});

it("switches between original and P6 results, reopens saved history and plays fixtures without posting", async () => {
  const api = mockBackend((id) => frame(1, "tool_result", { request_id: requestId, status: "EXECUTED", cached: id === "run_2" })
    + frame(2, "completed", { verdict: "ALLOW" }));
  const view = render(<DemoAgentPage />);
  fireEvent.click((await lab()).getAllByRole("button", { name: "Run" })[0]);
  expect(await screen.findByRole("heading", { name: "Paid" })).toBeVisible();
  fireEvent.click((await lab()).getAllByRole("button", { name: "Run" })[1]);
  expect(await screen.findByRole("heading", { name: "Original payment confirmed" })).toBeVisible();
  click(normal.prompt);
  expect(screen.getByRole("heading", { name: "Paid" })).toBeVisible();
  click(retry.prompt);
  view.unmount();
  render(<DemoAgentPage />);
  expect(screen.getByRole("heading", { name: "Original payment confirmed" })).toBeVisible();
  const sheet = await lab();
  fireEvent.click(sheet.getByRole("tab", { name: "Attacks" }));
  fireEvent.click(sheet.getByRole("button", { name: "Identity and request integrity" }));
  fireEvent.click(sheet.getByRole("button", { name: "View fixture" }));
  expect(screen.getByText("Recorded fixture · Read-only")).toBeVisible();
  expect(api.posts.map((post) => post.scenario_id)).toEqual(["P1", "P6"]);
  expect(api.gets).toHaveLength(2);
});
