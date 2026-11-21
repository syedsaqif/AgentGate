import { strToU8, zipSync } from "fflate";
import verifierHtml from "../../../verifier/dispute-verifier-v1.html?raw";
import { ApiError } from "../../api/errors";
import { isRecord } from "../../api/shellWire";
import { coverageText, makeQrDataUrl, receiptFacts } from "../../components/qrReceipt";
export { verifierUrl } from "../../components/qrReceipt";

function escapeHtml(text: string) {
  return text.replace(/[&<>"']/g, value => (
    { "&": "&amp;", "<": "&lt;", ">": "&gt;", "\"": "&quot;", "'": "&#39;" }[value] ?? value
  ));
}

export function parseProof(proof: string, requestId: string) {
  const value: unknown = JSON.parse(proof);
  if (!isRecord(value) || value.format !== "agentgate-dispute-v1" || !isRecord(value.record)
    || value.record.request_id !== requestId || !isRecord(value.evidence)) {
    throw new ApiError("The gateway returned an invalid proof export.", "UI_INVALID_RESPONSE");
  }
  return { ...receiptFacts(requestId, value.evidence.receipt_seq, value.evidence.receipt_hash,
    value.witness_checkpoint === null ? null : isRecord(value.witness_checkpoint) ? value.witness_checkpoint.seq : undefined),
    fixture: value.fixture === true };
}

export async function downloadDispute(proof: string, url: string, requestId: string, signal: AbortSignal) {
  const { seq, hash, coverage, fixture } = parseProof(proof, requestId);
  if (fixture) {
    throw new ApiError("Mock receipt fixtures do not contain a signed proof.", "UI_INVALID_RESPONSE");
  }
  const qr = await makeQrDataUrl(url, 360);
  const receipt = '<!doctype html><html lang="en"><meta charset="utf-8">' +
    '<meta http-equiv="Content-Security-Policy" content="default-src \'none\'; img-src data:; style-src \'unsafe-inline\'">' +
    '<meta name="viewport" content="width=device-width, initial-scale=1">' +
    '<title>AgentGate dispute receipt</title><style>body{font:16px/1.5 system-ui;margin:2rem auto;max-width:42rem;' +
    'padding:0 1rem;color:#111;background:#fff;overflow-wrap:anywhere}dt{font-weight:bold}dd{margin:0 0 .5rem}img{max-width:100%}</style>' +
    '<h1>AgentGate dispute receipt</h1><dl><dt>Request ID</dt><dd>' + escapeHtml(requestId) +
    '</dd><dt>Customer audit sequence</dt><dd>' + String(seq) +
    '</dd><dt>Customer audit hash</dt><dd>' + hash +
    '</dd><dt>Witness coverage</dt><dd>' + coverageText(coverage) +
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
