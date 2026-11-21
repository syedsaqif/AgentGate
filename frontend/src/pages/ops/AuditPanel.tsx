import { useEffect, useState, type FormEvent } from "react";
import { auditFilterKeys, type AuditFilters, type AuditPage, type EvidenceBundle } from "../../api/auditWire";
import type { ShellApi } from "../../api/client";
import type { ApiError } from "../../api/errors";
import { ErrorNotice } from "../../components/ErrorNotice";
import { formatKolkataTimestamp } from "../../formatting";
import { AuditVerification, VerificationReport } from "./AuditVerification";
import { useAuditRead } from "./useAuditRead";
import { DisputeExport } from "./DisputeExport";

type AuditProps = { api: ShellApi; token: string; endSession: (error: ApiError) => void };
type Selection = { bundle: EvidenceBundle; json: string };

const filterLabels: Record<typeof auditFilterKeys[number], string> = {
  event_type: "Event type", request_id: "Request ID", customer_id: "Customer ID", agent_id: "Agent ID",
};

export function AuditPanel({ api, token, endSession }: AuditProps) {
  const list = useAuditRead<AuditPage>(endSession);
  const evidence = useAuditRead<Selection>(endSession);
  const { run } = list;
  const [filters, setFilters] = useState<AuditFilters>({});
  const [cursor, setCursor] = useState<string | null>(null);
  const [selected, setSelected] = useState<string | null>(null);

  useEffect(() => {
    let cancelled = false;
    queueMicrotask(() => { if (!cancelled) void run((signal) => api.auditEntries(token, {}, null, signal)); });
    return () => { cancelled = true; };
  }, [api, token, run]);

  function readPage(activeFilters: AuditFilters, next: string | null) {
    setFilters(activeFilters);
    setCursor(next);
    setSelected(null);
    evidence.clear();
    void run((signal) => api.auditEntries(token, activeFilters, next, signal));
  }

  function apply(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const data = new FormData(event.currentTarget);
    const next: AuditFilters = {};
    for (const key of auditFilterKeys) {
      const value = String(data.get(key) ?? "").trim();
      if (value) next[key] = value;
    }
    readPage(next, null);
  }

  function openEvidence(requestId: string) {
    setSelected(requestId);
    void evidence.run((signal) => api.auditEvidence(token, requestId, signal));
  }

  const page = list.state.kind === "ready" ? list.state.data : null;
  const loading = list.state.kind === "loading" || list.state.kind === "idle";
  return (
    <section className="panel audit-panel" aria-labelledby="ops-audit-title">
      <h2 id="ops-audit-title">Audit</h2>
      <section aria-labelledby="audit-entries-title">
        <h3 id="audit-entries-title">Audit entries</h3>
        <form onSubmit={apply} aria-label="Audit entry filters">
          <div className="mandate-fields">
            {auditFilterKeys.map((key) => <label key={key}>{filterLabels[key]}<input name={key} /></label>)}
          </div>
          <div className="mandate-buttons audit-actions">
            <button type="submit">Apply filters</button>
            <button type="button" className="secondary" onClick={(event) => {
              event.currentTarget.form?.reset();
              readPage({}, null);
            }}>Clear filters</button>
          </div>
        </form>
        <div aria-busy={loading}>
          {loading && <p role="status">Loading audit entries…</p>}
          {list.state.kind === "error" && (
            <>
              <ErrorNotice error={list.state.error} />
              <button type="button" className="secondary" onClick={() => readPage(filters, cursor)}>Retry audit entries</button>
            </>
          )}
          {page && (page.items.length === 0 ? <p>No audit entries match these filters.</p> : (
            <div className="audit-table-wrap">
              <table>
                <thead><tr><th scope="col">Sequence</th><th scope="col">Event</th><th scope="col">Request ID</th>
                  <th scope="col">Time (IST)</th><th scope="col">Details</th></tr></thead>
                <tbody>
                  {page.items.map((entry, index) => {
                    const requestId = entry.request_id;
                    return (
                      <tr key={entry.seq ?? index}>
                        <td>{entry.seq ?? "—"}</td><td>{entry.event_type ?? "—"}</td>
                        <td>{requestId ?? "—"}</td>
                        <td>{entry.occurred_at ? (
                          <time dateTime={entry.occurred_at}>{formatKolkataTimestamp(entry.occurred_at)}</time>
                        ) : "—"}</td>
                        <td>
                          {requestId && <button type="button" className="secondary"
                            aria-label={`View evidence for ${requestId} from sequence ${entry.seq ?? "unavailable"}`}
                            onClick={() => openEvidence(requestId)}>View evidence</button>}
                          <details><summary>Redacted record</summary><pre>{JSON.stringify(entry, null, 2)}</pre></details>
                        </td>
                      </tr>
                    );
                  })}
                </tbody>
              </table>
            </div>
          ))}
          {page && <div className="mandate-buttons audit-actions">
            {cursor !== null && <button type="button" className="secondary" onClick={() => readPage(filters, null)}>First audit page</button>}
            {page.next_cursor !== null && <button type="button" className="secondary"
              onClick={() => readPage(filters, page.next_cursor)}>Next audit page</button>}
          </div>}
        </div>
      </section>

      {selected !== null && (
        <section aria-labelledby="audit-evidence-title" aria-busy={evidence.state.kind === "loading"}>
          <h3 id="audit-evidence-title">Evidence for {selected}</h3>
          {evidence.state.kind === "loading" && <p role="status">Loading evidence…</p>}
          {evidence.state.kind === "error" && <ErrorNotice error={evidence.state.error} />}
          {evidence.state.kind === "ready" && evidence.state.data.bundle.request_id === selected && (
            <>
              <EvidenceDetails key={`evidence-${selected}`} evidence={evidence.state.data} />
              <DisputeExport key={`dispute-${selected}`} api={api} token={token}
                endSession={endSession} requestId={selected} />
            </>
          )}
        </section>
      )}

      <AuditVerification api={api} token={token} endSession={endSession} />
    </section>
  );
}

function EvidenceDetails({ evidence }: { evidence: Selection }) {
  const [url, setUrl] = useState("");
  useEffect(() => {
    const next = URL.createObjectURL(new Blob([evidence.json], { type: "application/json" }));
    setUrl(next);
    return () => URL.revokeObjectURL(next);
  }, [evidence]);
  const requestId = evidence.bundle.request_id.replace(/[^a-zA-Z0-9_-]/g, "_").slice(0, 80);
  return (
    <>
      {url && <a href={url} download={`audit-evidence-${requestId}.json`}>Download evidence JSON</a>}
      <details open><summary>Provider evidence bundle</summary><pre>{evidence.json}</pre></details>
      <h4>Verification scope at export</h4>
      <VerificationReport report={evidence.bundle.verification} />
    </>
  );
}
