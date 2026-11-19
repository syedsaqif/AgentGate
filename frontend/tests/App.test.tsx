import { act, fireEvent, render, screen, within } from "@testing-library/react";
import { describe, expect, it, vi } from "vitest";
import { App } from "../src/App";
import { createShellApi } from "../src/api/client";
import { createGatewayTransport } from "../src/api/gatewayTransport";
import { mandatePath } from "../src/api/mandateWire";
import { shellRoutes, type Role } from "../src/api/shellWire";
import { demoCredentials, demoIds, demoSessionTtlMs } from "../src/mocks/fixtures";
import { createMockTransport } from "../src/mocks/transport";

const customer = { role: "customer", idLabel: "Customer ID", secretLabel: "PIN", heading: "Your accounts" } as const;
const ops = { role: "ops", idLabel: "Operator ID", secretLabel: "Password", heading: "Registered agents" } as const;

function visit(role: Role) {
  window.history.replaceState(null, "", `/${role}`);
}

function enterSecret(label: string, secret: string) {
  fireEvent.change(screen.getByLabelText(label), { target: { value: secret } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));
}

function renderMock(role: Role, transport = createMockTransport()) {
  visit(role);
  return render(<App mode="mock" api={createShellApi(transport)} demoIds={demoIds} />);
}

describe("fixture-backed workspaces", () => {
  it.each([
    { ...customer, name: "accepts PIN 4821 and loads bootstrap", id: "cust_001", secret: "4821" },
    { ...ops, name: "signs in and reads the Ops workspace", ...demoCredentials.ops },
  ])("$name", async ({ role, idLabel, secretLabel, heading, id, secret }) => {
    const transport = createMockTransport();
    const request = vi.spyOn(transport, "request");
    renderMock(role, transport);

    expect(screen.getByText("Mock mode · Fixture data")).toBeVisible();
    expect(screen.getByLabelText(idLabel)).toHaveValue(id);
    expect(screen.getByLabelText(secretLabel)).toHaveValue("");
    enterSecret(secretLabel, secret);

    expect(await screen.findByRole("heading", { name: heading })).toBeVisible();
    expect(screen.getByText("BillPal")).toBeVisible();
    if (role === "customer") {
      expect(within(screen.getByRole("region", { name: "Your accounts" }))
        .getByText("Checking •••• 001")).toBeVisible();
      expect(screen.queryByText(/ACC-001/)).not.toBeInTheDocument();
      expect(screen.getByText("Electricity Board")).toBeVisible();
      expect(screen.queryByText("Budget Assistant")).not.toBeInTheDocument();
      expect(await screen.findByText("No mandates yet.")).toBeVisible();
    } else {
      const row = within(screen.getByText("Budget Assistant").closest("li")!);
      expect(row.getByText("SUSPENDED")).toBeVisible();
    }
    expect(request.mock.calls.map(([path, init]) => [path, init.method])).toEqual([
      [shellRoutes[role].login, "POST"],
      [shellRoutes[role].read, "GET"],
      ...(role === "customer" ? [[mandatePath, "GET"]] : []),
    ]);
    const [login, read] = request.mock.calls.map(([, init]) => new Headers(init.headers));
    expect(login.get("Authorization")).toBeNull();
    expect(read.get("Authorization")).toMatch(/^Bearer \S+$/);
    expect(window.location.search).toBe("");
    expect(window.sessionStorage.length).toBe(0);
    expect(window.localStorage.length).toBe(0);

    fireEvent.click(screen.getByRole("button", { name: "Sign out" }));
    expect(screen.queryByRole("heading", { name: heading })).not.toBeInTheDocument();
    expect(screen.getByLabelText(secretLabel)).toHaveValue("");
  });

  it.each([
    { ...customer, name: "rejects PIN 0426 and hides bootstrap", secret: "0426" },
    { ...ops, name: "shows the contract error for invalid Ops credentials", secret: "incorrect" },
  ])("$name", async ({ role, secretLabel, heading, secret }) => {
    const transport = createMockTransport();
    const request = vi.spyOn(transport, "request");
    renderMock(role, transport);
    enterSecret(secretLabel, secret);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Invalid sign-in credentials.");
    expect(alert).toHaveTextContent("E_AUTH_INVALID_TOKEN");
    expect(alert).toHaveTextContent("req_mock_1");
    expect(screen.getByLabelText(secretLabel)).toHaveValue("");
    expect(screen.queryByRole("heading", { name: heading })).not.toBeInTheDocument();
    expect(screen.queryByText("BillPal")).not.toBeInTheDocument();
    expect(request.mock.calls.map(([path, init]) => [path, init.method])).toEqual([
      [shellRoutes[role].login, "POST"],
    ]);
  });

  it.each([
    { ...customer, emptyMessage: "No accounts available." },
    { ...ops, emptyMessage: "No registered agents." },
  ])("shows empty $role output", async ({ role, secretLabel, emptyMessage }) => {
    renderMock(role, createMockTransport({ readScenario: "empty" }));
    enterSecret(secretLabel, demoCredentials[role].secret);
    expect(await screen.findByText(emptyMessage)).toBeVisible();
    if (role === "customer") {
      expect(screen.getByText("No billers available.")).toBeVisible();
      expect(screen.getByText("No agents available.")).toBeVisible();
    }
  });

  it("shows a protected-read contract error", async () => {
    renderMock("ops", createMockTransport({ readScenario: "unavailable" }));
    enterSecret("Password", demoCredentials.ops.secret);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("The workspace data is temporarily unavailable.");
    expect(alert).toHaveTextContent("E_SERVICE_UNAVAILABLE");
    expect(alert).toHaveTextContent("req_mock_2");
    expect(screen.queryByRole("heading", { name: "Registered agents" })).not.toBeInTheDocument();
  });

  it("shows loading and discards a protected read that finishes after logout", async () => {
    const mock = createMockTransport();
    let finishRead: (() => void) | undefined;
    const transport = {
      async request(path: string, init: RequestInit) {
        const response = await mock.request(path, init);
        if (path === shellRoutes.customer.read) {
          await new Promise<void>((resolve) => { finishRead = resolve; });
        }
        return response;
      },
    };
    renderMock("customer", transport);
    enterSecret("PIN", demoCredentials.customer.secret);

    expect(await screen.findByText("Loading workspace…")).toBeVisible();
    const signOut = screen.getByRole("button", { name: "Sign out" });
    fireEvent.click(signOut);
    await act(async () => finishRead?.());
    expect(screen.getByRole("heading", { name: "Customer sign-in" })).toBeVisible();
    expect(screen.queryByText("BillPal")).not.toBeInTheDocument();
  });

  it("clears loaded data when the in-memory session expires", async () => {
    vi.useFakeTimers({ toFake: ["Date", "setTimeout", "clearTimeout"] });
    renderMock("customer");
    await act(async () => enterSecret("PIN", demoCredentials.customer.secret));
    expect(screen.getByText("BillPal")).toBeVisible();

    await act(async () => { await vi.advanceTimersByTimeAsync(demoSessionTtlMs); });
    expect(screen.getByRole("alert")).toHaveTextContent("UI_SESSION_EXPIRED");
    expect(screen.getByRole("heading", { name: "Customer sign-in" })).toBeVisible();
    expect(screen.queryByText("BillPal")).not.toBeInTheDocument();
  });

  it("returns to sign-in with the request reference when the gateway rejects a session", async () => {
    let time = Date.now();
    const mock = createMockTransport({ now: () => time });
    const transport = {
      request(path: string, init: RequestInit) {
        if (path === shellRoutes.ops.read) time += demoSessionTtlMs;
        return mock.request(path, init);
      },
    };
    renderMock("ops", transport);
    enterSecret("Password", demoCredentials.ops.secret);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("E_AUTH_EXPIRED");
    expect(alert).toHaveTextContent("req_mock_2");
    expect(screen.getByRole("heading", { name: "Ops sign-in" })).toBeVisible();
    expect(screen.queryByText("BillPal")).not.toBeInTheDocument();
  });

  it("requires sign-in when the app mounts again", async () => {
    const runtime = createMockTransport();
    const firstVisit = renderMock("customer", runtime);
    enterSecret("PIN", demoCredentials.customer.secret);
    expect(await screen.findByText("BillPal")).toBeVisible();
    firstVisit.unmount();

    renderMock("customer", runtime);
    expect(screen.getByRole("heading", { name: "Customer sign-in" })).toBeVisible();
    expect(screen.queryByText("BillPal")).not.toBeInTheDocument();
  });
});

describe("real gateway mode", () => {
  it("surfaces a connection failure with no fixture fallback", async () => {
    const fetch = vi.fn().mockRejectedValue(new TypeError("Failed to fetch"));
    vi.stubGlobal("fetch", fetch);
    const origin = "http://127.0.0.1:8000";
    visit("customer");
    render(<App mode="real" api={createShellApi(createGatewayTransport(origin))} />);

    expect(screen.getByLabelText("Customer ID")).toHaveValue("");
    expect(screen.getByLabelText("PIN")).toHaveValue("");
    fireEvent.change(screen.getByLabelText("Customer ID"), { target: { value: demoIds.customer } });
    enterSecret("PIN", demoCredentials.customer.secret);

    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("Could not connect to the gateway.");
    expect(alert).toHaveTextContent("UI_GATEWAY_UNREACHABLE");
    expect(screen.queryByText("Mock mode", { exact: false })).not.toBeInTheDocument();
    expect(screen.queryByText("BillPal")).not.toBeInTheDocument();
    expect(fetch).toHaveBeenCalledExactlyOnceWith(`${origin}${shellRoutes.customer.login}`,
      expect.objectContaining({ method: "POST", credentials: "omit", cache: "no-store", redirect: "error" }));
  });
});
