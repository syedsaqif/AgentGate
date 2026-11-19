import type { VerifyReport } from "../../api/auditWire";
import type { ShellApi } from "../../api/client";
import type { ApiError } from "../../api/errors";
import { ErrorNotice } from "../../components/ErrorNotice";
import { useAuditRead } from "./useAuditRead";

export function AuditVerification({ api, token, endSession }: {
  api: ShellApi; token: string; endSession: (error: ApiError) => void;
}) {
  const { state, run } = useAuditRead<VerifyReport>(endSession);
  return (
    <section aria-labelledby="audit-verify-title" aria-busy={state.kind === "loading"}>
      <h3 id="audit-verify-title">Chain verification</h3>
      <button type="button" className="secondary" disabled={state.kind === "loading"}
        onClick={() => void run((signal) => api.verifyAudit(token, signal))}>Verify audit chain</button>
      {state.kind === "loading" && <p role="status">Verifying audit chain…</p>}
      {state.kind === "error" && (
        <>
          <p>Verification unavailable.</p>
          <ErrorNotice error={state.error} />
        </>
      )}
      {state.kind === "ready" && <VerificationReport report={state.data} />}
    </section>
  );
}

export function VerificationReport({ report }: { report: VerifyReport }) {
  const { valid, entries_checked, head_seq, head_hash, anchored_through_seq, first_failure } = report;
  return (
    <div className="audit-report">
      <p role="status"><strong>{valid ? "Verification passed." : "Provider reported an integrity failure."}</strong></p>
      <dl>
        <dt>Entries checked</dt><dd>{entries_checked}</dd>
        <dt>Reported head sequence</dt><dd>{head_seq}</dd>
        <dt>Reported head hash</dt><dd><code>{head_hash}</code></dd>
      </dl>
      {valid && head_seq > 0 && head_seq === entries_checked && <p>Checked range #1–#{head_seq}.</p>}
      {first_failure && <p>First failure at sequence #{first_failure.seq} · <code>{first_failure.reason}</code></p>}
      {anchored_through_seq > 0
        ? <p>Retained checkpoint coverage through sequence #{anchored_through_seq}.</p>
        : <p>No retained checkpoint coverage confirmed.</p>}
      {valid && anchored_through_seq < head_seq && (
        <p>Internally consistent suffix #{anchored_through_seq + 1}–#{head_seq} extends beyond retained checkpoint coverage.
          Tail deletion in that suffix can go undetected.</p>
      )}
    </div>
  );
}
