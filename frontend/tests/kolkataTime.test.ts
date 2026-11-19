import { describe, expect, it } from "vitest";
import { formatKolkataTimestamp, parseKolkataExpiry } from "../src/formatting";

describe("Kolkata mandate expiry", () => {
  it.each([
    ["2027-01-01 00:15", "2026-12-31T18:45:00.000Z", "2027-01-01 00:15:00 IST"],
    ["2028-03-01T00:00", "2028-02-29T18:30:00.000Z", "2028-03-01 00:00:00 IST"],
    ["2027-01-17T14:30:25", "2027-01-17T09:00:25.000Z", "2027-01-17 14:30:25 IST"],
  ])("round-trips %s through UTC", (input, utc, display) => {
    expect(parseKolkataExpiry(input)).toEqual({ ok: true, value: utc });
    expect(formatKolkataTimestamp(utc)).toBe(display);
  });

  it.each(["", "2027-02-29T12:00", "2027-04-31T12:00", "2027-01-01T24:00", "2027-01-01T22:60"])(
    "rejects invalid local input %j",
    (input) => expect(parseKolkataExpiry(input)).toEqual({
      ok: false,
      error: expect.stringMatching(/\S/),
    }),
  );
});

describe("gateway timestamp display", () => {
  it("labels a UTC timestamp with no fractional seconds as IST", () => {
    expect(formatKolkataTimestamp("2026-12-31T18:45:00Z")).toBe("2027-01-01 00:15:00 IST");
  });

  it.each(["2027-02-29T12:00:00Z", "2027-01-01T00:00:00", "2027-01-01T00:00:00+05:30"])(
    "rejects invalid gateway timestamp %s",
    (timestamp) => expect(() => formatKolkataTimestamp(timestamp)).toThrow(RangeError),
  );
});
