import { describe, expect, it } from "vitest";
import { formatInrPaise, parseMandateCapPaise } from "../src/formatting";

describe("mandate cap input", () => {
  it.each([
    ["0.01", 1],
    ["1850.50", 185050],
    ["15", 1500],
    [" 2.5 ", 250],
    ["90071992547409.91", Number.MAX_SAFE_INTEGER],
  ])("converts %s rupees to %d paise", (input, paise) => {
    expect(parseMandateCapPaise(input)).toEqual({ ok: true, value: paise });
  });

  it.each(["", "   ", "-0.01", "1.005", "1e3", "0", "0.00", "90071992547409.92"])(
    "rejects %j with a validation message",
    (input) => expect(parseMandateCapPaise(input)).toEqual({
      ok: false,
      error: expect.stringMatching(/\S/),
    }),
  );
});

describe("INR display", () => {
  it.each([
    [0, "₹0.00"],
    [1, "₹0.01"],
    [185050, "₹1,850.50"],
    [Number.MAX_SAFE_INTEGER, "₹9,00,71,99,25,47,409.91"],
  ])("formats %d paise as %s", (paise, display) => {
    expect(formatInrPaise(paise)).toBe(display);
  });

  it.each([-1, 1.5, Infinity, Number.MAX_SAFE_INTEGER + 1])("rejects invalid paise %d", (paise) => {
    expect(() => formatInrPaise(paise)).toThrow(RangeError);
  });
});
