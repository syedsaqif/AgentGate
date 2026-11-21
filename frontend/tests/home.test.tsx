import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "../src/App";
import { createShellApi } from "../src/api/client";
import { createMockTransport } from "../src/mocks/transport";
import { demoIds } from "../src/mocks/fixtures";

describe("HomePage", () => {
  it("renders homepage at root path with links to demo, demo/bank, and demo/agent and no customer/ops links", () => {
    window.history.replaceState(null, "", "/");
    render(<App mode="mock" api={createShellApi(createMockTransport())} demoIds={demoIds} />);

    expect(screen.getByRole("heading", { name: "Autonomous Financial Safety Gateway" })).toBeVisible();

    const stageLink = screen.getByRole("link", { name: /Presenter Stage/i });
    expect(stageLink).toHaveAttribute("href", "/demo");

    const bankLink = screen.getByRole("link", { name: /Demo Bank/i });
    expect(bankLink).toHaveAttribute("href", "/demo/bank");

    const agentLink = screen.getByRole("link", { name: /Payment Assistant/i });
    expect(agentLink).toHaveAttribute("href", "/demo/agent");

    // Ensure customer and operations links are excluded
    expect(screen.queryByRole("link", { name: /^Customer$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /^Ops$/i })).not.toBeInTheDocument();
    expect(screen.queryByRole("link", { name: /operations/i })).not.toBeInTheDocument();
  });
});
