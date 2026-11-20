import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { auditEntriesPath, auditVerifyPath } from "../src/api/auditWire";
import { createShellApi } from "../src/api/client";
import type { ApiTransport } from "../src/api/types";
import { evidenceFixtures } from "../src/mocks/audit";
import { demoCredentials, demoIds, demoSessionTtlMs } from "../src/mocks/fixtures";
import { createMockTransport } from "../src/mocks/transport";

const defaultTime = Date.parse("2026-11-17T04:00:00Z");
const firstId = "req_mock_s11";
const secondId = "req_mock_s11_followup";
const firstEvidence = `/v1/audit/evidence/${firstId}`;
const secondEvidence = `/v1/audit/evidence/${secondId}`;

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(defaultTime);
});

const entries = () => within(screen.getByRole("region", { name: "Audit entries" }));
const verifier = () => within(screen.getByRole("region", { name: "Chain verification" }));
const evidence = (id: string) => within(screen.getByRole("region", { name: `Evidence for ${id}` }));

async function signIn(transport: ApiTransport) {
  window.history.replaceState(null, "", "/ops");
  render(<App mode="mock" api={createShellApi(transport)} demoIds={demoIds} />);
  fireEvent.change(screen.getByLabelText("Password"), { target: { value: demoCredentials.ops.secret } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
  expect(await screen.findByRole("heading", { name: "Audit" })).toBeVisible();
  expect(await entries().findByRole("table")).toBeVisible();
}

function setFilter(label: string, value: string) {
  fireEvent.change(entries().getByLabelText(label), { target: { value } });
}

function applyFilters() {
  fireEvent.click(entries().getByRole("button", { name: "Apply filters" }));
}

function openEvidence(id: string, seq: number) {
  fireEvent.click(entries().getByRole("button", { name: `View evidence for ${id} from sequence ${seq}` }));
}

function verify() {
  fireEvent.click(verifier().getByRole("button", { name: "Verify audit chain" }));
}

function unavailable(requestId: string) {
  return Response.json({ error: {
    code: "E_AUDIT_WRITE_FAILED", message: "Audit provider unavailable.", request_id: requestId,
  } }, { status: 503 });
}

describe("Ops audit entries", () => {
  it("filters with B's exact-match fields, follows opaque cursors, and resets the cursor when applying filters", async () => {
    const mock = createMockTransport();
    const request = vi.fn((path: string, init: RequestInit) => mock.request(path.startsWith(auditEntriesPath)
      ? path + (path.includes("?") ? "&" : "?") + "limit=1" : path, init));
    await signIn({ request });
    expect(entries().getByRole("cell", { name: "agent.registered" })).toBeVisible();
    expect(entries().getByRole("cell", { name: "2026-11-17 09:30:00 IST" })).toBeVisible();
    fireEvent.click(entries().getByRole("button", { name: "Next audit page" }));
    expect(await entries().findByRole("cell", { name: "policy.decision" })).toBeVisible();
    expect(new URL(request.mock.calls.at(-1)![0], "http://fixture.invalid").searchParams.get("cursor")).toBe("audit after:1");

    setFilter("Event type", "bank.executed");
    setFilter("Agent ID", "agt_billpal");
    setFilter("Customer ID", "cust_001");
    applyFilters();
    expect(await entries().findByRole("cell", { name: "5" })).toBeVisible();
    expect(entries().getByRole("cell", { name: firstId })).toBeVisible();
    expect(entries().getByRole("cell", { name: "2026-11-17 09:32:00 IST" })).toBeVisible();
    fireEvent.click(entries().getByText("Redacted record"));
    expect(entries().getByText(/\[REDACTED\]/)).toBeVisible();
    const [url, init] = request.mock.calls.at(-1)!;
    expect(Object.fromEntries(new URL(url, "http://fixture.invalid").searchParams)).toEqual({
      event_type: "bank.executed", agent_id: "agt_billpal", customer_id: "cust_001",
    });
    expect(Boolean(new Headers(init.headers).get("Authorization")?.startsWith("Bearer "))).toBe(true);
    fireEvent.click(entries().getByRole("button", { name: "Next audit page" }));
    expect(await entries().findByRole("cell", { name: secondId })).toBeVisible();
    const next = new URL(request.mock.calls.at(-1)![0], "http://fixture.invalid");
    expect(next.searchParams.get("cursor")).toBe("audit after:5");
    expect(next.searchParams.get("event_type")).toBe("bank.executed");

    setFilter("Request ID", "req_missing");
    applyFilters();
    expect(await entries().findByText("No audit entries match these filters.")).toBeVisible();
    expect(new URL(request.mock.calls.at(-1)![0], "http://fixture.invalid").searchParams.get("request_id")).toBe("req_missing");
  });

  it("aborts a previous filter request and discards its late response", async () => {
    const mock = createMockTransport();
    let release: (() => void) | undefined;
    const request = vi.fn(async (path: string, init: RequestInit) => {
      const response = await mock.request(path, init);
      if (path.includes("event_type=policy.decision")) await new Promise<void>((resolve) => { release = resolve; });
      return response;
    });
    await signIn({ request });
    setFilter("Event type", "policy.decision");
    await act(async () => applyFilters());
    expect(entries().getByText("Loading audit entries…")).toBeVisible();
    const old = request.mock.calls.at(-1)![1].signal;
    setFilter("Event type", "bank.executed");
    applyFilters();
    expect(await entries().findByRole("cell", { name: "5" })).toBeVisible();
    expect(old?.aborted).toBe(true);
    await act(async () => release?.());
    expect(entries().getAllByRole("cell", { name: "bank.executed" })).toHaveLength(2);
    expect(entries().queryByRole("cell", { name: "policy.decision" })).not.toBeInTheDocument();
  });

  it("shows list errors and retries with the applied filters", async () => {
    const mock = createMockTransport();
    let fail = true;
    const request = vi.fn((path: string, init: RequestInit) => {
      if (path.includes("event_type=bank.executed") && fail) {
        fail = false;
        return Promise.resolve(unavailable("req_audit_list_unavailable"));
      }
      return mock.request(path, init);
    });
    await signIn({ request });
    setFilter("Event type", "bank.executed");
    applyFilters();
    expect(await entries().findByRole("alert")).toHaveTextContent("req_audit_list_unavailable");
    expect(entries().queryByRole("table")).not.toBeInTheDocument();
    fireEvent.click(entries().getByRole("button", { name: "Retry audit entries" }));
    expect(await entries().findByRole("cell", { name: "5" })).toBeVisible();
    expect(request.mock.calls.at(-1)![0]).toBe(`${auditEntriesPath}?event_type=bank.executed`);
  });
});

describe("Ops audit verification reports", () => {
  it("runs on demand and separates the anchored prefix from a verified suffix", async () => {
    const mock = createMockTransport();
    const request = vi.spyOn(mock, "request");
    await signIn(mock);
    expect(request.mock.calls.some(([path]) => path === auditVerifyPath)).toBe(false);
    verify();
    expect(await verifier().findByText("Verification passed.")).toBeVisible();
    expect(verifier().getByText("Checked range #1–#8.")).toBeVisible();
    expect(verifier().getByText("Reported head sequence")).toBeVisible();
    expect(verifier().getByText("8".repeat(64))).toBeVisible();
    expect(verifier().getByText("Retained checkpoint coverage through sequence #5.")).toBeVisible();
    expect(verifier().getByText(/Internally consistent suffix #6–#8/)).toHaveTextContent("Tail deletion in that suffix can go undetected.");
    expect(request.mock.calls.filter(([path]) => path === auditVerifyPath).map(([, init]) => init.method)).toEqual(["GET"]);
  });

  it.each([
    { variant: "edited", seq: 2, reason: "entry_hash_mismatch" },
    { variant: "gap", seq: 3, reason: "sequence_gap: expected seq 3, found 4" },
    { variant: "signature", seq: 2, reason: "invalid_signature" },
    { variant: "truncated", seq: 5, reason: "tail_truncation: head_seq 4 is below anchored seq 5" },
  ] as const)("shows the first reported failure for $variant", async ({ variant, seq, reason }) => {
    await signIn(createMockTransport({ auditVerification: variant }));
    verify();
    expect(await verifier().findByText("Provider reported an integrity failure.")).toBeVisible();
    const failure = verifier().getByText(`First failure at sequence #${seq}`, { exact: false });
    expect(failure).toHaveTextContent(reason);
    expect(verifier().queryByText(/Internally consistent suffix/)).not.toBeInTheDocument();
  });

  it("clears an earlier success when a new verification starts and shows unavailability on error", async () => {
    const mock = createMockTransport();
    let attempt = 0;
    let finish: (() => void) | undefined;
    const request: ApiTransport["request"] = async (path, init) => {
      if (path === auditVerifyPath && ++attempt === 2) {
        await new Promise<void>((resolve) => { finish = resolve; });
        return unavailable("req_verify_unavailable");
      }
      return mock.request(path, init);
    };
    await signIn({ request });
    verify();
    expect(await verifier().findByText("Verification passed.")).toBeVisible();
    verify();
    expect(verifier().getByText("Verifying audit chain…")).toBeVisible();
    expect(verifier().queryByText("Verification passed.")).not.toBeInTheDocument();
    await act(async () => finish?.());
    expect(verifier().getByText("Verification unavailable.")).toBeVisible();
    expect(verifier().getByRole("alert")).toHaveTextContent("req_verify_unavailable");
    expect(verifier().queryByText("Provider reported an integrity failure.")).not.toBeInTheDocument();
    expect(verifier().queryByText("Verification passed.")).not.toBeInTheDocument();
  });
});

function captureDownloads() {
  const urls = new Map<string, Blob>();
  const create = vi.fn((blob: Blob) => {
    const url = `blob:audit-fixture-${urls.size + 1}`;
    urls.set(url, blob);
    return url;
  });
  const revoke = vi.fn();
  vi.stubGlobal("URL", class extends URL {
    static createObjectURL = create;
    static revokeObjectURL = revoke;
  });
  return { urls, revoke };
}

function blobText(blob: Blob) {
  return new Promise<string>((resolve, reject) => {
    const reader = new FileReader();
    reader.onload = () => resolve(String(reader.result));
    reader.onerror = () => reject(reader.error);
    reader.readAsText(blob);
  });
}

describe("Ops audit evidence", () => {
  it("downloads the actual selected provider JSON with historical state and verification scope", async () => {
    const { urls, revoke } = captureDownloads();
    const mock = createMockTransport();
    const raw = JSON.stringify(evidenceFixtures[firstId], null, 2) + "\n";
    const request: ApiTransport["request"] = (path, init) => path === firstEvidence
      ? Promise.resolve(new Response(raw, { headers: { "Content-Type": "application/json" } }))
      : mock.request(path, init);
    await signIn({ request });
    const registered = within(screen.getByRole("region", { name: "Registered agents" }));
    expect(await registered.findByText("BillPal")).toBeVisible();
    openEvidence(firstId, 2);
    const link = await evidence(firstId).findByRole("link", { name: "Download evidence JSON" });
    expect(evidence(firstId).getByText(/BillPal v1 \(historical\)/)).toBeVisible();
    expect(evidence(firstId).getByText("Verification scope at export")).toBeVisible();
    expect(evidence(firstId).getByText(/Retained checkpoint coverage through sequence #5/)).toBeVisible();
    let clickedUrl: string | null = null;
    link.addEventListener("click", (event) => {
      event.preventDefault();
      clickedUrl = link.getAttribute("href");
    }, { once: true });
    fireEvent.click(link);
    expect(link).toHaveAttribute("download", `audit-evidence-${firstId}.json`);
    const blob = urls.get(clickedUrl ?? "")!;
    expect(blob.type).toBe("application/json");
    const actual = await blobText(blob);
    expect(actual).toBe(raw);
    expect(JSON.parse(actual)).toEqual(evidenceFixtures[firstId]);
    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(revoke).toHaveBeenCalledWith(clickedUrl);
    expect(screen.queryByRole("link", { name: "Download evidence JSON" })).not.toBeInTheDocument();
  });

  it("revokes old links and ignores late evidence after switching request IDs", async () => {
    const { revoke } = captureDownloads();
    const mock = createMockTransport();
    let attempts = 0;
    let release: (() => void) | undefined;
    const request = vi.fn(async (path: string, init: RequestInit) => {
      const response = await mock.request(path, init);
      if (path === firstEvidence && ++attempts === 2) await new Promise<void>((resolve) => { release = resolve; });
      return response;
    });
    await signIn({ request });
    openEvidence(firstId, 2);
    const firstLink = await evidence(firstId).findByRole("link", { name: "Download evidence JSON" });
    const previousUrl = firstLink.getAttribute("href");
    await act(async () => openEvidence(firstId, 2));
    expect(evidence(firstId).getByText("Loading evidence…")).toBeVisible();
    expect(screen.queryByRole("link", { name: "Download evidence JSON" })).not.toBeInTheDocument();
    expect(revoke).toHaveBeenCalledWith(previousUrl);
    const slow = request.mock.calls.at(-1)![1].signal;

    openEvidence(secondId, 6);
    const link = await evidence(secondId).findByRole("link", { name: "Download evidence JSON" });
    expect(slow?.aborted).toBe(true);
    await act(async () => release?.());
    expect(link).toHaveAttribute("download", `audit-evidence-${secondId}.json`);
    expect(evidence(secondId).getByText(/BillPal v2 \(historical\)/)).toBeVisible();
    expect(screen.queryByText(/BillPal v1 \(historical\)/)).not.toBeInTheDocument();
  });

  it("clears a previous download when the new evidence selection fails", async () => {
    const { revoke } = captureDownloads();
    const mock = createMockTransport();
    await signIn({
      request: (path, init) => path === secondEvidence
        ? Promise.resolve(Response.json({ error: {
          code: "E_REQ_INVALID", message: "Audit evidence not found.", request_id: "req_evidence_missing",
        } }, { status: 404 })) : mock.request(path, init),
    });
    openEvidence(firstId, 2);
    const oldUrl = (await evidence(firstId).findByRole("link", { name: "Download evidence JSON" })).getAttribute("href");
    openEvidence(secondId, 6);
    expect(await evidence(secondId).findByRole("alert")).toHaveTextContent("req_evidence_missing");
    expect(revoke).toHaveBeenCalledWith(oldUrl);
    expect(screen.queryByRole("link", { name: "Download evidence JSON" })).not.toBeInTheDocument();
    expect(screen.queryByText(/BillPal v1 \(historical\)/)).not.toBeInTheDocument();
  });

  it("removes a loaded bundle and its download when the provider reports session expiry", async () => {
    const { revoke } = captureDownloads();
    let clock = defaultTime;
    await signIn(createMockTransport({ now: () => clock }));
    openEvidence(firstId, 2);
    const oldUrl = (await evidence(firstId).findByRole("link", { name: "Download evidence JSON" })).getAttribute("href");
    clock += demoSessionTtlMs;
    verify();
    expect(await screen.findByRole("heading", { name: "Ops sign-in" })).toBeVisible();
    expect(screen.queryByRole("heading", { name: /Evidence for/ })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: "Download evidence JSON" })).not.toBeInTheDocument();
    expect(revoke).toHaveBeenCalledWith(oldUrl);
  });

  it("rejects all three audit reads with a Customer session and keeps Audit out of Customer UI", async () => {
    const mock = createMockTransport();
    const api = createShellApi(mock);
    const { id, secret } = demoCredentials.customer;
    const { signal } = new AbortController();
    const customer = await api.login("customer", id, secret, signal);
    for (const path of [auditEntriesPath, auditVerifyPath, firstEvidence]) {
      const response = await mock.request(path, {
        method: "GET", headers: { Authorization: `Bearer ${customer.session_token}` },
      });
      expect(response.status).toBe(401);
    }
    window.history.replaceState(null, "", "/customer");
    render(<App mode="mock" api={api} demoIds={demoIds} />);
    fireEvent.change(screen.getByLabelText("PIN"), { target: { value: secret } });
    fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
    expect(await screen.findByText("No mandates yet.")).toBeVisible();
    expect(screen.queryByRole("region", { name: "Audit" })).not.toBeInTheDocument();
  });
});
