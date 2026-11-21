import { useEffect, useId, useRef, useState } from "react";
import { createPortal } from "react-dom";
import { ApiError } from "../api/errors";
import { ErrorNotice } from "./ErrorNotice";
import { coverageText, makeQrDataUrl, type ReceiptFacts } from "./qrReceipt";
import "./qrDialog.css";

export type QrSource = ReceiptFacts & {
  url: string;
  fixture?: boolean;
  downloadZip?: (signal: AbortSignal) => Promise<void>;
};
type DialogState = { kind: "loading" } | { kind: "error"; error: ApiError }
  | { kind: "ready"; source: QrSource; png: string };

function asError(error: unknown) {
  return error instanceof ApiError ? error : new ApiError("Could not prepare the receipt QR.", "UI_REQUEST_FAILED");
}

export function QrDialog({ state, onClose, onZip }: { state: DialogState; onClose: () => void; onZip: () => Promise<void> }) {
  const ref = useRef<HTMLDialogElement>(null);
  const title = useId();
  const [feedback, setFeedback] = useState("");
  const [saving, setSaving] = useState(false);
  useEffect(() => {
    if (ref.current && !ref.current.open) ref.current.showModal();
  }, []);

  async function copy(url: string) {
    try {
      await navigator.clipboard.writeText(url);
      setFeedback("Verifier link copied.");
    } catch {
      setFeedback("Could not copy automatically. Select the link to copy it.");
    }
  }

  async function zip() {
    setSaving(true);
    try { await onZip(); }
    catch (error) { setFeedback(asError(error).message); }
    finally { setSaving(false); }
  }

  const ready = state.kind === "ready" ? state : undefined;
  const source = ready?.source;
  return createPortal(<dialog ref={ref} aria-labelledby={title} onClose={onClose} className="qr-dialog">
    <div className="qr-content">
      <h2 id={title}>Verify this payment offline</h2>
      {state.kind === "loading" && <>
        <div className="qr-tile qr-placeholder" aria-hidden="true" />
        <p role="status">Preparing QR…</p>
      </>}
      {state.kind === "error" && <ErrorNotice error={state.error} />}
      {ready && source && <>
        {source.fixture && <p>Mock receipt · Fixture data</p>}
        <div className="qr-tile"><img src={ready.png} width={512} height={512}
          alt="QR code that opens the AgentGate dispute verifier" /></div>
        <dl>
          <dt>Request ID</dt><dd>{source.requestId}</dd>
          <dt>Audit sequence</dt><dd>{source.seq}</dd>
          <dt>Audit hash</dt><dd><code>{source.hash}</code></dd>
          <dt>Witness coverage</dt><dd>{coverageText(source.coverage)}</dd>
        </dl>
        <p>{source.url}</p>
        <p>Scan to open the verifier. Every receipt uses the same verifier link. The payment evidence comes from <code>proof.json</code>.</p>
      </>}
      <div className="qr-actions">
        <button type="button" autoFocus onClick={() => ref.current?.close()}>Close</button>
        {ready && source && <>
          <a download={`agentgate-verifier-qr-${source.requestId}.png`} href={ready.png}>Save PNG</a>
          <button type="button" onClick={() => void copy(source.url)}>Copy link</button>
          <button type="button" onClick={() => window.print()}>Print</button>
          {source.downloadZip && <button type="button" disabled={saving} onClick={() => void zip()}>Download full proof ZIP</button>}
        </>}
      </div>
      {feedback && <p className="qr-feedback" role="status">{feedback}</p>}
    </div>
  </dialog>, document.body);
}

export function QrButton({ load, disabled, onUnauthorized }: {
  load: (signal: AbortSignal) => Promise<QrSource>; disabled?: boolean; onUnauthorized?: (error: ApiError) => void;
}) {
  const trigger = useRef<HTMLButtonElement>(null);
  const operation = useRef<AbortController | null>(null);
  const [state, setState] = useState<DialogState>();
  useEffect(() => () => operation.current?.abort(), []);

  async function open() {
    if (operation.current) return;
    const controller = new AbortController();
    operation.current = controller;
    setState({ kind: "loading" });
    try {
      const source = await load(controller.signal);
      controller.signal.throwIfAborted();
      const png = await makeQrDataUrl(source.url, 512);
      if (!controller.signal.aborted) setState({ kind: "ready", source, png });
    } catch (failure) {
      if (controller.signal.aborted) return;
      const error = asError(failure);
      if (error.status === 401 && onUnauthorized) onUnauthorized(error);
      else setState({ kind: "error", error });
    }
  }

  return <>
    <button ref={trigger} type="button" disabled={disabled} onClick={() => void open()}>Download QR code</button>
    {state && <QrDialog state={state} onClose={() => {
      operation.current?.abort();
      operation.current = null;
      setState(undefined);
      trigger.current?.focus();
    }} onZip={async () => {
      if (state.kind === "ready" && operation.current) await state.source.downloadZip?.(operation.current.signal);
    }} />}
  </>;
}
