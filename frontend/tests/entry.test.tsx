import { act, fireEvent, screen } from "@testing-library/react";
import type { Root } from "react-dom/client";
import { afterEach, beforeEach, expect, it, vi } from "vitest";
import { demoCredentials } from "../src/mocks/fixtures";

const { roots } = vi.hoisted(() => ({ roots: [] as Root[] }));

vi.mock("react-dom/client", async (importOriginal) => {
  const actual = await importOriginal<typeof import("react-dom/client")>();
  return {
    ...actual,
    createRoot(container: HTMLElement) {
      const root = actual.createRoot(container);
      roots.push(root);
      return root;
    },
  };
});

beforeEach(() => {
  vi.resetModules();
  document.body.innerHTML = '<div id="root"></div>';
});

afterEach(() => {
  act(() => roots.splice(0).forEach((root) => root.unmount()));
  document.body.replaceChildren();
  vi.unstubAllEnvs();
});

it.each([
  { role: "customer", secretLabel: "PIN", result: "Your accounts" },
  { role: "ops", secretLabel: "Password", result: "Registered agents" },
] as const)("starts the $role flow through src/main.tsx in mock mode", async ({ role, secretLabel, result }) => {
  vi.stubEnv("MODE", "mock");
  window.history.replaceState(null, "", `/${role}`);

  await act(async () => {
    await import("../src/main");
    await vi.dynamicImportSettled();
  });
  expect(screen.getByText("Mock mode · Fixture data")).toBeVisible();
  fireEvent.change(screen.getByLabelText(secretLabel), { target: { value: demoCredentials[role].secret } });
  fireEvent.click(screen.getByRole("button", { name: "Sign in" }));

  expect(await screen.findByRole("heading", { name: result })).toBeVisible();
  if (role === "customer") {
    expect(await screen.findByText("No mandates yet.")).toBeVisible();
    expect(await screen.findByRole("button", { name: "Approve" })).toBeEnabled();
  } else {
    expect(await screen.findByText("req_ops_demo_review")).toBeVisible();
    expect(await screen.findByRole("heading", { name: "Audit entries" })).toBeVisible();
    expect(await screen.findByRole("button", { name: "Verify audit chain" })).toBeEnabled();
  }
});

it("shows a configuration failure through src/main.tsx in real mode", async () => {
  vi.stubEnv("MODE", "real");
  vi.stubEnv("VITE_GATEWAY_ORIGIN", undefined);
  window.history.replaceState(null, "", "/customer");

  await act(async () => { await import("../src/main"); });
  expect(screen.getByRole("alert")).toHaveTextContent("UI_GATEWAY_CONFIG");
  expect(screen.queryByRole("button", { name: "Sign in" })).not.toBeInTheDocument();
  expect(screen.queryByText("Mock mode", { exact: false })).not.toBeInTheDocument();
});
