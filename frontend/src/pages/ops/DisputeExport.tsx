import type { ShellApi } from "../../api/client";
import { ApiError } from "../../api/errors";
import { ErrorNotice } from "../../components/ErrorNotice";
import { useAuditRead } from "./useAuditRead";

export function DisputeExport({ api, token, endSession, requestId }: {
  api: ShellApi;
  token: string;
  endSession: (error: ApiError) => void;
  requestId: string;
}) {
  const { state, run } = useAuditRead<boolean>(endSession);
  return (
    <>
      <button type="button" disabled={state.kind === "loading"} onClick={() => void run(async signal => {
        const { downloadDispute, verifierUrl } = await import("./disputeFiles");
        const url = verifierUrl(import.meta.env.VITE_DISPUTE_VERIFIER_URL);
        const proof = await api.disputeProof(token, requestId, signal);
        await downloadDispute(proof, url, requestId, signal);
        return true;
      })}>Export Court-Ready Proof</button>
      {state.kind === "loading" && <p role="status">Preparing proof files…</p>}
      {state.kind === "ready" && <p role="status">Proof archive prepared for download.</p>}
      {state.kind === "error" && <ErrorNotice error={state.error} />}
    </>
  );
}
