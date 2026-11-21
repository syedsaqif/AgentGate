import { toDataURL } from "qrcode";
import { ApiError } from "../api/errors";
import { isReceipt } from "../api/approvalWire";
import { isRecord } from "../api/shellWire";

export function verifierUrl(configured: string | undefined) {
  const url = URL.parse(configured ?? "");
  if (!url || url.protocol !== "https:" || url.username || url.password || url.search || url.hash
    || url.href.includes("?") || url.href.includes("#")
    || !url.pathname.endsWith("/dispute-verifier-v1.html") || url.href.length > 256) {
    throw new ApiError("Set a public, versioned HTTPS verifier URL without a query or fragment.", "UI_INVALID_CONFIG");
  }
  return url.href;
}

export type ReceiptFacts = { requestId: string; seq: number; hash: string; coverage: number | null };
export const coverageText = (seq: number | null) => seq === null
  ? "External witness checkpoint unavailable for this export"
  : "External witness checkpoint included through sequence " + seq;

export function makeQrDataUrl(url: string, width: number) {
  return toDataURL(url, { errorCorrectionLevel: "M", width, margin: 4 });
}

export function receiptFacts(requestId: string, seq: unknown, hash: unknown, coverage: unknown): ReceiptFacts {
  const ref = { seq, entry_hash: hash };
  if (!isReceipt(ref) || (coverage !== null && (typeof coverage !== "number"
    || !Number.isSafeInteger(coverage) || coverage <= ref.seq))) {
    throw new ApiError("The gateway returned an invalid proof export.", "UI_INVALID_RESPONSE");
  }
  return { requestId, seq: ref.seq, hash: ref.entry_hash, coverage };
}

export function readQrSummary(value: unknown, requestId: string) {
  if (!isRecord(value) || value.request_id !== requestId) {
    throw new ApiError("The bank returned an invalid receipt.", "UI_INVALID_RESPONSE");
  }
  const url = verifierUrl(typeof value.verifier_url === "string" ? value.verifier_url : undefined);
  return { ...receiptFacts(requestId, value.receipt_seq, value.receipt_hash, value.witness_coverage), url };
}
