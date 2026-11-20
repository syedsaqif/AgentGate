import { strToU8, zipSync } from "fflate";
import { toDataURL } from "qrcode";
import verifierHtml from "../../../verifier/dispute-verifier-v1.html?raw";
import { ApiError } from "../../api/errors";
import { isRecord } from "../../api/shellWire";

export function verifierUrl(configured: string | undefined) {
  const url = URL.parse(configured ?? "");
  if (!url || url.protocol !== "https:" || url.username || url.password || url.search || url.hash
    || url.href.includes("?") || url.href.includes("#")
    || !url.pathname.endsWith("/dispute-verifier-v1.html") || url.href.length > 256) {
    throw new ApiError("Set a public, versioned HTTPS verifier URL without a query or fragment.", "UI_INVALID_CONFIG");
  }
  return url.href;
}

function escapeHtml(text: string) {
  return text.replace(/[&<>"']/g, value => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[value] ?? value
  ));
}

export async function downloadDispute(proof: string, url: string, requestId: string, signal: AbortSignal) {
  const value: unknown = JSON.parse(proof);
  if (!isRecord(value) || value.format !== "agentgate-dispute-v1" || !isRecord(value.record)
    || value.record.request_id !== requestId || !isRecord(value.evidence)
    || typeof value.evidence.receipt_hash !== "string" || !/^[a-f0-9]{64}$/.test(value.evidence.receipt_hash)
    || !Number.isSafeInteger(value.evidence.receipt_seq)
    || (value.witness_checkpoint !== null
      && (!isRecord(value.witness_checkpoint) || !Number.isSafeInteger(value.witness_checkpoint.seq)))) {
    throw new ApiError("The gateway returned an invalid proof export.", "UI_INVALID_RESPONSE");
  }
  const coverage = value.witness_checkpoint === null
    ? "External witness checkpoint unavailable for this export"
    : "External witness checkpoint included through sequence " + String(value.witness_checkpoint.seq);
  const qr = await toDataURL(url, { errorCorrectionLevel: "M", width: 360, margin: 4 });
  const receipt = '<!doctype html><html lang="en"><meta charset="utf-8">' +
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>AgentGate dispute receipt</title><style>body{font:16px/1.5 system-ui;margin:2rem auto;max-width:42rem;' +
    'padding:0 1rem;color:#111;background:#fff;overflow-wrap:anywhere}dt{font-weight:bold}dd{margin:0 0 .5rem}img{max-width:100%}</style>' +
    '<h1>AgentGate dispute receipt</h1><dl><dt>Request ID</dt><dd>' + escapeHtml(requestId) +
    '</dd><dt>Customer audit sequence</dt><dd>' + String(value.evidence.receipt_seq) +
    '</dd><dt>Customer audit hash</dt><dd>' + value.evidence.receipt_hash +
    '</dd><dt>Witness coverage</dt><dd>' + coverage +
    '</dd></dl><img alt="QR to the standalone verifier, release v1" src="' + qr +
    '"><p>' + escapeHtml(url) + '</p><p>Scan to open the verifier. Confirm the release and trust fingerprint independently, ' +
    'then import the proof package. Use your browser print command for this receipt.</p></html>';

  signal.throwIfAborted();
  const zip = zipSync({
    "receipt.html": strToU8(receipt),
    "proof.json": strToU8(proof),
    "dispute-verifier-v1.html": strToU8(verifierHtml),
  });
  const blobUrl = URL.createObjectURL(new Blob([new Uint8Array(zip)], { type: "application/zip" }));
  const link = document.createElement("a");
  link.href = blobUrl;
  link.download = "agentgate-dispute-" + requestId + ".zip";
  document.body.append(link);
  link.click();
  link.remove();
  setTimeout(() => URL.revokeObjectURL(blobUrl), 1000);
}
