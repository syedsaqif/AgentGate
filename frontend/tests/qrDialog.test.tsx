import { fireEvent, render, screen, within } from "@testing-library/react";
import { afterAll, beforeAll, describe, expect, it, vi } from "vitest";
import { createShellApi } from "../src/api/client";
import { QrButton } from "../src/components/QrDialog";
import { readQrSummary } from "../src/components/qrReceipt";
import { demoCredentials } from "../src/mocks/fixtures";
import { createMockTransport } from "../src/mocks/transport";
import { DisputeExport } from "../src/pages/ops/DisputeExport";

vi.mock("qrcode", () => ({ toDataURL: vi.fn(async () => "data:image/png;base64,fixture") }));

const dialogPrototype = HTMLDialogElement.prototype;
const originalShowModal = Object.getOwnPropertyDescriptor(dialogPrototype, "showModal");
beforeAll(() => {
  if (!originalShowModal) Object.defineProperty(dialogPrototype, "showModal", {
    configurable: true, value(this: HTMLDialogElement) { this.open = true; },
  });
});
afterAll(() => {
  if (!originalShowModal) Reflect.deleteProperty(dialogPrototype, "showModal");
});

describe("receipt QR dialog", () => {
  it("shows loading immediately and reports invalid verifier configuration", async () => {
    render(<QrButton load={async () => readQrSummary({ request_id: "req_test",
      verifier_url: "http://example.test/dispute-verifier-v1.html" }, "req_test")} />);
    fireEvent.click(screen.getByRole("button", { name: "Download QR code" }));
    expect(screen.getByRole("status")).toHaveTextContent("Preparing QR");
    const alert = await screen.findByRole("alert");
    expect(alert).toHaveTextContent("UI_INVALID_CONFIG");
    expect(screen.queryByRole("link", { name: "Save PNG" })).not.toBeInTheDocument();
  });

  it("uses the authenticated mock transport and labels the receipt fixture", async () => {
    vi.stubEnv("MODE", "mock");
    vi.stubEnv("VITE_DISPUTE_VERIFIER_URL", "https://example.test/dispute-verifier-v1.html");
    const api = createShellApi(createMockTransport());
    const { id, secret } = demoCredentials.ops;
    const session = await api.login("ops", id, secret, new AbortController().signal);
    render(<DisputeExport api={api} token={session.session_token} endSession={vi.fn()}
      requestId="req_mock_s11" executed />);
    fireEvent.click(screen.getByRole("button", { name: "Download QR code" }));
    const dialog = within(await screen.findByRole("dialog", { name: "Verify this payment offline" }));
    expect(await dialog.findByText("Mock receipt · Fixture data")).toBeInTheDocument();
    expect(dialog.getByText("External witness checkpoint unavailable for this export")).toBeInTheDocument();
    expect(dialog.getByRole("link", { name: "Save PNG" })).toHaveAttribute("download", "agentgate-verifier-qr-req_mock_s11.png");
    expect(dialog.queryByRole("button", { name: "Download full proof ZIP" })).not.toBeInTheDocument();
  });

  it("keeps QR unavailable when an Ops decision has no executed payment", () => {
    render(<DisputeExport api={createShellApi(createMockTransport())} token="test" endSession={vi.fn()}
      requestId="req_pending" executed={false} />);
    expect(screen.getByRole("button", { name: "Download QR code" })).toBeDisabled();
    expect(screen.getByText("No payment was made, so there is nothing to prove.")).toBeInTheDocument();
  });
});
