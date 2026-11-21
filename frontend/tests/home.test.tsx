import { fireEvent, render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "../src/App";
import { createShellApi } from "../src/api/client";
import { createMockTransport } from "../src/mocks/transport";
import { demoIds } from "../src/mocks/fixtures";

describe("HomePage", () => {
  it("shows one preview and destination link for the selected tab", () => {
    window.history.replaceState(null, "", "/");
    render(<App mode="mock" api={createShellApi(createMockTransport())} demoIds={demoIds} />);

    expect(screen.getByRole("heading", { name: "See how an AI payment gets approved." })).toBeVisible();
    expect(screen.getByRole("tab", { name: "Presenter Stage" })).toHaveAttribute("aria-selected", "true");
    for (const [name, route, preview] of [
      ["Presenter Stage", "/demo", "Payment journey"],
      ["Demo Bank", "/demo/bank", "Agent mandate"],
      ["Payment Assistant", "/demo/agent", "BillPal conversation"],
      ["Payment Gate", "/demo/gate", "Evaluation stages"],
    ]) {
      fireEvent.click(screen.getByRole("tab", { name }));
      expect(screen.getByRole("tabpanel", { name })).toBeVisible();
      expect(screen.getByRole("heading", { name: preview })).toBeVisible();
      expect(screen.getAllByRole("link")).toHaveLength(1);
      expect(screen.getByRole("link", { name: "Open " + name })).toHaveAttribute("href", route);
    }

    fireEvent.keyDown(screen.getByRole("tab", { selected: true }), { key: "ArrowRight" });
    expect(screen.getByRole("tab", { name: "Presenter Stage" })).toHaveFocus();
  });
});
