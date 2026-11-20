import { act, fireEvent, render, screen, waitFor, within } from "@testing-library/react";
import { beforeEach, describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { createShellApi } from "../src/api/client";
import { mandatePath, type CreateMandateBody } from "../src/api/mandateWire";
import type { ApiTransport } from "../src/api/types";
import { customerBootstrap, demoCredentials, demoIds, demoSessionTtlMs } from "../src/mocks/fixtures";
import { createMockTransport } from "../src/mocks/transport";

const seed = demoCredentials.customer;
const fixtureNow = Date.parse("2026-12-31T18:00:00Z");

beforeEach(() => {
  vi.useFakeTimers({ toFake: ["Date"] });
  vi.setSystemTime(fixtureNow);
});

function change(label: string, value: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value } });
}

function click(name: string) {
  fireEvent.click(screen.getByRole("button", { name }));
}

function mandateList() {
  return within(screen.getByRole("region", { name: "Your mandates" }));
}

async function signIn(transport: ApiTransport) {
  window.history.replaceState(null, "", "/customer");
  render(<App mode="mock" api={createShellApi(transport)} demoIds={demoIds} />);
  change("PIN", seed.secret);
  click("Sign in");
  expect(await screen.findByRole("heading", { name: "Your mandates" })).toBeVisible();
  await waitFor(() => expect(screen.getByRole("button", { name: "Review mandate" })).toBeEnabled());
}

function fillDraft(perTxn = "1850.50", lifetime = "5000.01") {
  change("Account", "ACC-001");
  change("Agent", "agt_billpal");
  fireEvent.click(screen.getByRole("checkbox", { name: "Pay bills" }));
  fireEvent.click(screen.getByRole("checkbox", { name: "Electricity Board" }));
  change("Per-transaction cap (INR)", perTxn);
  change("Mandate-lifetime cap (INR)", lifetime);
  change("Count limit per grant", "3");
  change("Expiry (IST)", "2027-01-01T00:15");
}

function confirm(pin = seed.secret) {
  change("Confirmation PIN", pin);
  click("Confirm mandate");
}

async function createThroughUi(lifetime?: string) {
  fillDraft("1850.50", lifetime);
  click("Review mandate");
  confirm();
  await waitFor(() => expect(screen.queryByLabelText("Confirmation PIN")).not.toBeInTheDocument());
}

describe("Customer mandate UI against fixtures", () => {
  it("reviews and creates exact terms, then shows the new grant and usage after GET", async () => {
    const transport = createMockTransport();
    const request = vi.spyOn(transport, "request");
    await signIn(transport);
    expect(mandateList().getByText("No mandates yet.")).toBeVisible();
    fillDraft();
    expect(screen.queryByRole("checkbox", { name: "Transfer funds" })).not.toBeInTheDocument();
    expect(screen.getByText("fixture-billpal-key-fingerprint")).toBeVisible();
    fireEvent.click(screen.getByRole("checkbox", { name: "Read balance" }));
    fireEvent.click(screen.getByText("Optional time and approval terms"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Fri" }));
    change("Window starts (IST)", "09:00");
    change("Window ends (IST)", "17:00");
    change("Approval threshold (INR)", "0");
    fireEvent.click(screen.getByRole("checkbox", { name: "Always ask to read balance" }));

    click("Review mandate");
    expect(screen.getByRole("heading", { name: "Review grant terms" })).toBeVisible();
    expect(screen.getByText("Per transaction ₹1,850.50")).toBeVisible();
    expect(screen.getByText("2027-01-01 00:15:00 IST")).toBeVisible();
    expect(screen.getByText("Approval above ₹0.00")).toBeVisible();
    expect(request.mock.calls.filter(([path, init]) => path === mandatePath && init.method === "POST")).toHaveLength(0);

    change("Confirmation PIN", seed.secret);
    const submit = screen.getByRole("button", { name: "Confirm mandate" });
    act(() => {
      fireEvent.click(submit);
      fireEvent.click(submit);
    });
    expect(screen.getByLabelText("Confirmation PIN")).toHaveValue("");
    expect(await mandateList().findByText("ACTIVE", { exact: false })).toBeVisible();
    const list = mandateList();
    expect(list.getByText("2027-01-01 00:15:00 IST")).toBeVisible();
    expect(list.getByText("₹5,000.01 · 3 uses")).toBeVisible();
    expect(list.getAllByText("Committed")).toHaveLength(2);
    expect(list.getAllByText("Reserved")).toHaveLength(2);
    expect(list.queryByText(/ACC-001/)).not.toBeInTheDocument();
    expect(screen.queryByRole("alert")).not.toBeInTheDocument();

    const mandateCalls = request.mock.calls.filter(([path]) => path === mandatePath);
    expect(mandateCalls.map(([, init]) => init.method)).toEqual(["GET", "POST", "GET"]);
    const create = mandateCalls[1][1];
    expect(Boolean(new Headers(create.headers).get("Authorization")?.startsWith("Bearer "))).toBe(true);
    const { pin, ...terms } = JSON.parse(String(create.body));
    expect(pin === seed.secret).toBe(true);
    const timeWindow = { timezone: "Asia/Kolkata", weekdays: [5], start_local: "09:00", end_local: "17:00" };
    expect(terms).toEqual({
      agent_id: "agt_billpal",
      valid_from: "2026-12-31T18:00:00.000Z",
      valid_until: "2026-12-31T18:45:00.000Z",
      risk_profile: "balanced",
      grants: [
        { grant_id: "grant_bill_pay", action: "bill_pay", account_ids: ["ACC-001"], payee_ids: ["BILL-ELEC"], currency: "INR",
          max_per_txn_paise: 185050, max_total_paise: 500001, max_count: 3, window: "mandate", time_window: timeWindow },
        { grant_id: "grant_balance_read", action: "balance_read", account_ids: ["ACC-001"], max_count: 3,
          window: "mandate", time_window: timeWindow },
      ],
      step_up: { amount_above_paise: 0, new_payee_requires_approval: true, always_ask_actions: ["balance_read"] },
    });
  });

  it("allows optional window starts and ends when weekdays are selected", async () => {
    const transport = createMockTransport();
    await signIn(transport);
    fillDraft();
    fireEvent.click(screen.getByText("Optional time and approval terms"));
    fireEvent.click(screen.getByRole("checkbox", { name: "Tue" }));
    // Do NOT fill window starts or window ends (leave optional)
    click("Review mandate");
    expect(screen.getByRole("heading", { name: "Review grant terms" })).toBeVisible();
    expect(screen.getByText("Tue 00:00–23:59 IST")).toBeVisible();
  });

  it("blocks invalid limits before confirmation and sends no create request", async () => {
    const transport = createMockTransport();
    const request = vi.spyOn(transport, "request");
    await signIn(transport);
    fillDraft("1.005");
    click("Review mandate");
    expect(screen.getByRole("alert")).toHaveTextContent("two decimal places");
    expect(screen.queryByLabelText("Confirmation PIN")).not.toBeInTheDocument();
    expect(request.mock.calls.some(([path, init]) => path === mandatePath && init.method === "POST")).toBe(false);
  });

  it("clears a wrong PIN and preserves the list", async () => {
    const transport = createMockTransport();
    const request = vi.spyOn(transport, "request");
    await signIn(transport);
    fillDraft();
    click("Review mandate");
    const wrongPin = "9157";
    confirm(wrongPin);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_ESC_BAD_PIN");
    expect(alert).toHaveTextContent("Request ID");
    expect(alert.textContent?.includes(wrongPin)).toBe(false);
    expect(screen.getByLabelText("Confirmation PIN")).toHaveValue("");
    expect(mandateList().getByText("No mandates yet.")).toBeVisible();
    expect(request.mock.calls.filter(([path]) => path === mandatePath).map(([, init]) => init.method))
      .toEqual(["GET", "POST"]);
  });

  it("revokes once without a PIN and retries only GET after a failed refetch", async () => {
    const mock = createMockTransport();
    let releaseRevoke: (() => void) | undefined;
    let failRefresh = false;
    const worker = vi.fn(async (path: string, init: RequestInit) => {
      if (path.endsWith("/revoke")) {
        await new Promise<void>((resolve) => { releaseRevoke = resolve; });
        failRefresh = true;
      } else if (path === mandatePath && init.method === "GET" && failRefresh) {
        failRefresh = false;
        return Response.json({ error: {
          code: "E_SERVICE_UNAVAILABLE", message: "Mandate list unavailable.", request_id: "req_test_refresh",
        } }, { status: 503 });
      }
      return mock.request(path, init);
    });
    await signIn({ request: worker });
    await createThroughUi();
    const revoke = screen.getByRole("button", { name: "Revoke mandate" });
    act(() => {
      fireEvent.click(revoke);
      fireEvent.click(revoke);
    });
    expect(revoke).toBeDisabled();
    expect(screen.queryByLabelText("Confirmation PIN")).not.toBeInTheDocument();
    const mutations = () => worker.mock.calls.filter(([path]) => path.endsWith("/revoke"));
    expect(mutations()).toHaveLength(1);
    expect(mutations()[0][1].body).toBeUndefined();
    await act(async () => releaseRevoke?.());

    expect(await screen.findByText("Mandate revoked. The displayed list needs refreshing.")).toBeVisible();
    expect(mandateList().getByText("ACTIVE", { exact: false })).toBeVisible();
    expect(mandateList().queryByText(/^REVOKED/)).not.toBeInTheDocument();
    expect(screen.getByRole("alert")).toHaveTextContent("req_test_refresh");
    expect(revoke).toBeDisabled();

    click("Retry mandate list");
    expect(await mandateList().findByText(/^REVOKED/)).toBeVisible();
    expect(mutations()).toHaveLength(1);
    expect(worker.mock.calls.at(-1)?.[1].method).toBe("GET");
    expect(screen.queryByRole("button", { name: "Revoke mandate" })).not.toBeInTheDocument();
  });

  it("follows the server cursor and returns to the first page", async () => {
    const mock = createMockTransport();
    const request = vi.fn((path: string, init: RequestInit) => {
      if (init.method === "GET" && path.startsWith(mandatePath)) {
        return mock.request(path + (path.includes("?") ? "&" : "?") + "limit=1", init);
      }
      return mock.request(path, init);
    });
    await signIn({ request });
    await createThroughUi("4000");
    await createThroughUi("5000");
    expect(mandateList().getByText("₹5,000.00 · 3 uses")).toBeVisible();
    click("Next page");
    expect(await mandateList().findByText("₹4,000.00 · 3 uses")).toBeVisible();
    const get = request.mock.calls.at(-1);
    expect(new URL(get?.[0] ?? "", "http://mock.invalid").searchParams.get("cursor")).toBe("after:mnd_mock_2");
    click("First page");
    expect(await mandateList().findByText("₹5,000.00 · 3 uses")).toBeVisible();
  });

  it("clears the mandate view when revoke rejects the expired session", async () => {
    let time = fixtureNow;
    const mock = createMockTransport({ now: () => time });
    await signIn(mock);
    await createThroughUi();
    time += demoSessionTtlMs;
    click("Revoke mandate");
    expect(await screen.findByRole("heading", { name: "Customer sign-in" })).toBeVisible();
    expect(screen.getByRole("alert")).toHaveTextContent("E_AUTH_EXPIRED");
    expect(screen.queryByRole("heading", { name: "Your mandates" })).not.toBeInTheDocument();
  });

  it("removes an echoed confirmation PIN from a gateway-shaped error", async () => {
    const mock = createMockTransport();
    const confirmation = "7338";
    await signIn({
      request: (path, init) => path === mandatePath && init.method === "POST"
        ? Promise.resolve(Response.json({ error: {
          message: `Rejected input ${confirmation}`, code: "E_REQ_INVALID", request_id: "req_test_pin",
        } }, { status: 400 }))
        : mock.request(path, init),
    });
    fillDraft();
    click("Review mandate");
    confirm(confirmation);
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The gateway rejected the mandate request.");
    expect(alert).toHaveTextContent("E_REQ_INVALID");
    expect(alert.textContent?.includes(confirmation)).toBe(false);
    expect(screen.getByLabelText("Confirmation PIN")).toHaveValue("");
  });

  it("labels a missing fingerprint as unavailable while reviewing terms", async () => {
    const mock = createMockTransport();
    await signIn({
      request: (path, init) => path === "/v1/customer/bootstrap"
        ? Promise.resolve(Response.json({
          ...customerBootstrap, agents: [{ ...customerBootstrap.agents[0], jkt: undefined }],
        }))
        : mock.request(path, init),
    });
    fillDraft();
    click("Review mandate");
    const review = within(screen.getByRole("form", { name: "Review grant terms" }));
    expect(review.getByText("Unavailable")).toBeVisible();
  });
});

describe("mandate fixture authorization", () => {
  it("isolates bootstrap, lists, and revocation between two customer sessions", async () => {
    const other = { id: "cust_test_other", secret: "1906", bootstrap: {
      ...customerBootstrap, accounts: [{ account_id: "ACC-OTHER", masked_label: "Savings •••• 099" }],
    } };
    const api = createShellApi(createMockTransport({ additionalCustomers: [other] }));
    const { signal } = new AbortController();
    const first = await api.login("customer", seed.id, seed.secret, signal);
    const second = await api.login("customer", other.id, other.secret, signal);
    const body: CreateMandateBody = {
      agent_id: "agt_billpal", valid_from: new Date(fixtureNow).toISOString(),
      valid_until: "2026-12-31T18:45:00.000Z", risk_profile: "balanced", pin: seed.secret,
      grants: [{ grant_id: "grant_balance_read", action: "balance_read", account_ids: ["ACC-001"], max_count: 3, window: "mandate" }],
      step_up: { new_payee_requires_approval: true, always_ask_actions: [] },
    };
    await api.createMandate(first.session_token, body, signal);
    const ownerPage = await api.customerMandates(first.session_token, null, signal);
    expect(ownerPage.items).toHaveLength(1);
    expect(JSON.stringify(ownerPage).includes(seed.secret)).toBe(false);
    expect((await api.customerBootstrap(second.session_token, signal)).accounts.map((account) => account.account_id))
      .toEqual(["ACC-OTHER"]);
    expect(await api.customerMandates(second.session_token, null, signal)).toEqual({ items: [], next_cursor: null });
    await expect(api.createMandate(second.session_token, { ...body, pin: other.secret }, signal))
      .rejects.toMatchObject({ status: 400 });
    const id = ownerPage.items[0].terms.mandate_id;
    await expect(api.revokeMandate(second.session_token, id, signal)).rejects.toMatchObject({ status: 404 });
    expect((await api.customerMandates(first.session_token, null, signal)).items[0].status).toBe("ACTIVE");
  });

  it("projects mandate expiry from the fixture clock without expiring the session", async () => {
    let time = fixtureNow;
    const api = createShellApi(createMockTransport({ now: () => time }));
    const { signal } = new AbortController();
    const login = await api.login("customer", seed.id, seed.secret, signal);
    const expiry = fixtureNow + 60_000;
    await api.createMandate(login.session_token, {
      agent_id: "agt_billpal", valid_from: new Date(fixtureNow).toISOString(),
      valid_until: new Date(expiry).toISOString(), risk_profile: "balanced", pin: seed.secret,
      grants: [{ grant_id: "grant_balance_read", action: "balance_read", account_ids: ["ACC-001"], max_count: 1, window: "mandate" }],
      step_up: { new_payee_requires_approval: true, always_ask_actions: [] },
    }, signal);
    expect((await api.customerMandates(login.session_token, null, signal)).items[0].status).toBe("ACTIVE");
    time = expiry;
    expect((await api.customerMandates(login.session_token, null, signal)).items[0].status).toBe("EXPIRED");
  });
});
