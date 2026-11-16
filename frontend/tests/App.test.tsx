import { render, screen } from "@testing-library/react";
import { describe, expect, it } from "vitest";
import { App } from "../src/App";

describe("bootstrap shell", () => {
  it("clearly labels the repository as scaffold-only", () => {
    render(<App />);
    expect(screen.getByRole("status")).toHaveTextContent("IC0 scaffold only");
  });
});
