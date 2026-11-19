export type ValidationResult<T> = { ok: true; value: T } | { ok: false; error: string };

const inr = new Intl.NumberFormat("en-IN", {
  style: "currency",
  currency: "INR",
  minimumFractionDigits: 2,
  maximumFractionDigits: 2,
});

export function parseMandateCapPaise(input: string): ValidationResult<number> {
  const amount = /^(\d+)(?:\.(\d{1,2}))?$/.exec(input.trim());
  if (!amount) {
    return { ok: false, error: "Enter a rupee amount with up to two decimal places, such as 1850.50." };
  }

  const paise = Number(amount[1] + (amount[2] ?? "").padEnd(2, "0"));
  if (!Number.isSafeInteger(paise)) {
    return { ok: false, error: "This amount exceeds the supported paise range." };
  }
  if (paise === 0) {
    return { ok: false, error: "Enter a cap greater than zero." };
  }
  return { ok: true, value: paise };
}

export function formatInrPaise(paise: number): string {
  if (!Number.isSafeInteger(paise) || paise < 0) {
    throw new RangeError("Expected a nonnegative safe integer in paise.");
  }

  const units = BigInt(paise);
  const fraction = (units % 100n).toString().padStart(2, "0");
  return inr.formatToParts(units / 100n)
    .map((part) => part.type === "fraction" ? fraction : part.value)
    .join("");
}

const kolkataClock = new Intl.DateTimeFormat("en-GB", {
  timeZone: "Asia/Kolkata",
  calendar: "iso8601",
  numberingSystem: "latn",
  year: "numeric",
  month: "2-digit",
  day: "2-digit",
  hour: "2-digit",
  minute: "2-digit",
  second: "2-digit",
  hourCycle: "h23",
});

function kolkataWallTime(instant: Date): string {
  const { year, month, day, hour, minute, second } = Object.fromEntries(
    kolkataClock.formatToParts(instant).map(({ type, value }) => [type, value]),
  );
  return `${year.padStart(4, "0")}-${month}-${day}T${hour}:${minute}:${second}`;
}

function parseUtcTimestamp(timestamp: string): Date | null {
  if (!/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?Z$/.test(timestamp)
    || timestamp.startsWith("0000-")) return null;

  const instant = new Date(timestamp);
  if (!Number.isFinite(instant.getTime()) || instant.toISOString().slice(0, 19) !== timestamp.slice(0, 19)) {
    return null;
  }
  return instant;
}

export function formatKolkataTimestamp(timestamp: string): string {
  const instant = parseUtcTimestamp(timestamp);
  if (!instant) throw new RangeError("Expected a valid gateway UTC timestamp ending in Z.");
  return `${kolkataWallTime(instant).replace("T", " ")} IST`;
}

export function parseKolkataExpiry(input: string): ValidationResult<string> {
  const parts = /^(\d{4}-\d{2}-\d{2})[T ](\d{2}:\d{2})(:\d{2})?$/.exec(input.trim());
  const wallTime = parts ? `${parts[1]}T${parts[2]}${parts[3] ?? ":00"}` : "";
  const wallAsUtc = parseUtcTimestamp(`${wallTime}Z`);
  if (!wallAsUtc) return { ok: false, error: "Enter a valid Kolkata expiry date and time." };

  // Treat the wall time as UTC temporarily to calculate the Kolkata offset.
  const wallMs = wallAsUtc.getTime();
  const kolkataAtWallMs = Date.parse(`${kolkataWallTime(wallAsUtc)}Z`);
  const instant = new Date(wallMs - (kolkataAtWallMs - wallMs));
  if (!Number.isFinite(instant.getTime()) || instant.getUTCFullYear() < 1
    || kolkataWallTime(instant) !== wallTime) {
    return { ok: false, error: "This Kolkata expiry falls outside the supported UTC range." };
  }
  return { ok: true, value: instant.toISOString() };
}
