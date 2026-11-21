import type { ShellApi } from "../../api/client";
import { ApiError } from "../../api/errors";
import { ErrorNotice } from "../../components/ErrorNotice";
import { QrButton } from "../../components/QrDialog";
import { useAuditRead } from "./useAuditRead";

const defaultVerifierUrl = "https://syedsaqif.github.io/AgentGate/v1/dispute-verifier-v1.html";

export function DisputeExport({ api, token, endSession, requestId, executed }: {
  api: ShellApi;
  token: string;
  endSession: (error: ApiError) => void;
  requestId: string;
  executed: boolean;
}) {
  const { state, run } = useAuditRead<boolean>(endSession);
  const configuredVerifierUrl = import.meta.env.VITE_DISPUTE_VERIFIER_URL;
  return (
    <>
      <button type="button" disabled={state.kind === "loading"} onClick={() => void run(async signal => {
        const { downloadDispute, verifierUrl } = await import("./disputeFiles");
        const url = verifierUrl(configuredVerifierUrl ?? defaultVerifierUrl);
        const proof = await api.disputeProof(token, requestId, signal);
        await downloadDispute(proof, url, requestId, signal);
        return true;
      })}>Export Court-Ready Proof</button>
      <QrButton disabled={!executed} onUnauthorized={endSession} load={async (signal) => {
        const { downloadDispute, parseProof, verifierUrl } = await import("./disputeFiles");
        const mockMode = import.meta.env.MODE === "mock";
        const url = verifierUrl(configuredVerifierUrl ??
          (mockMode ? "https://example.invalid/dispute-verifier-v1.html" : defaultVerifierUrl));
        const proof = await api.disputeProof(token, requestId, signal);
        return { ...parseProof(proof, requestId), url, fixture: mockMode,
          downloadZip: mockMode ? undefined : (zipSignal) => downloadDispute(proof, url, requestId, zipSignal) };
      }} />
      {!executed && <p>No payment was made, so there is nothing to prove.</p>}
      {state.kind === "loading" && <p role="status">Preparing proof files…</p>}
      {state.kind === "ready" && <p role="status">Proof archive prepared for download.</p>}
      {state.kind === "error" && <ErrorNotice error={state.error} />}
    </>
  );
}
