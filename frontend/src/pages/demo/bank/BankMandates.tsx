import { useState, type FormEvent } from "react";
import type { MandateTermsInput } from "../../../api/mandateWire";
import { isRecord } from "../../../api/shellWire";
import { readAndClearPin } from "../../../components/pin";
import { formatInrPaise, formatKolkataTimestamp } from "../../../formatting";
import { actionLabels, readMandateDraft } from "../../customer/mandateDraft";
import { bankApi } from "./bankApi";
import { amountField, signingMetadata, stringField, type BankCatalog, type Overview } from "./bankWire";
import { blankDraft, exampleMandate, parseMandateLanguage, type BankDraft } from "./mandateLanguage";
import type { Mutation } from "./useBankWorkspace";

function mandateTerms(catalog: BankCatalog, grant: unknown, expiry: string, stepUp: unknown) {
  const listedPayees = isRecord(grant) && Array.isArray(grant.payee_ids)
    ? grant.payee_ids.filter((id): id is string => typeof id === "string") : [];
  const payeeNames = listedPayees.map((id) => catalog.payees.find((payee) => payee.payee_id === id)?.display_name ?? id);
  const cap = amountField(grant, "max_per_txn_paise");
  const total = amountField(grant, "max_total_paise");
  const count = amountField(grant, "max_count");
  const threshold = amountField(stepUp, "amount_above_paise");
  const firstPayee = isRecord(stepUp) ? stepUp.new_payee_requires_approval : undefined;
  return [
    ["Category", stringField(grant, "category") ?? "Any category"],
    ["Payee allow-list", payeeNames.join(", ") || "—"],
    ["Per payment", cap === undefined ? "—" : formatInrPaise(cap)],
    ["Total cap", total === undefined ? "—" : formatInrPaise(total)],
    ["Payment count cap", count ?? "—"],
    ["Expiry", formatKolkataTimestamp(expiry)],
    ["Ask me above", threshold === undefined ? "No amount threshold" : formatInrPaise(threshold)],
    ["Ask me the first time a payee is paid", typeof firstPayee === "boolean" ? firstPayee ? "On" : "Off" : "—"],
  ] as const;
}

function TermList({ catalog, grant, expiry, stepUp }: {
  catalog: BankCatalog; grant: unknown; expiry: string; stepUp: unknown;
}) {
  return <dl className="bank-terms">{mandateTerms(catalog, grant, expiry, stepUp).map(([label, value]) =>
    <div key={label}><dt>{label}</dt><dd>{value}</dd></div>)}</dl>;
}

function getStepUp(terms: unknown) {
  return isRecord(terms) ? terms.step_up : undefined;
}

export function MandateComposer({ overview, disabled, mutate, close }: {
  overview: Overview; disabled: boolean; mutate: Mutation; close: () => void;
}) {
  const [draft, setDraft] = useState<BankDraft>(blankDraft);
  const [language, setLanguage] = useState("");
  const [validation, setValidation] = useState("");
  const [preview, setPreview] = useState<MandateTermsInput>();
  const { catalog, accountId } = overview;
  const agents = catalog.agents.filter((agent) => agent.max_scopes?.includes("bill_pay"));
  const categories = [...new Set(catalog.payees.flatMap((payee) => payee.category ? [payee.category] : []))].sort();

  function edit<K extends keyof BankDraft>(key: K, value: BankDraft[K]) {
    setDraft((previous) => ({ ...previous, [key]: value }));
  }

  function parse() {
    const result = parseMandateLanguage(language, catalog);
    setValidation(result.ok ? "" : result.error);
    if (result.ok) setDraft(result.value);
  }

  function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const result = readMandateDraft(new FormData(event.currentTarget), catalog, ["bill_pay"]);
    setValidation(result.ok ? "" : result.error);
    if (result.ok) setPreview(result.value);
  }

  async function confirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const pin = readAndClearPin(event.currentTarget);
    if (!preview || disabled || !pin.trim()) return;
    if (Date.parse(preview.valid_until) <= Date.now()) {
      setValidation("Choose a future expiry before signing.");
      return;
    }
    if (await mutate(async (signal) => {
      await bankApi.create({ ...preview, pin }, signal);
      return "Mandate signed.";
    })) close();
  }

  const agentName = catalog.agents.find((agent) => agent.agent_id === preview?.agent_id)?.name;
  return (
    <section className="bank-card bank-composer" aria-labelledby="bank-composer-title">
      <h2 id="bank-composer-title">{preview ? "Review mandate" : "Write a mandate"}</h2>
      {validation && <p className="bank-error" role="alert">{validation}</p>}
      {preview ? (
        <form className="bank-form" autoComplete="off" method="post" onSubmit={(event) => void confirm(event)}>
          <fieldset disabled={disabled}>
            <p><strong>{agentName}</strong> may pay bills from {accountId}.</p>
            <TermList catalog={catalog} grant={preview.grants[0]} expiry={preview.valid_until} stepUp={preview.step_up} />
            <label>Confirmation PIN<input name="pin" type="password" inputMode="numeric" minLength={4} maxLength={16} required /></label>
            <div className="bank-actions">
              <button type="submit">Sign mandate</button>
              <button type="button" className="bank-secondary" onClick={() => { setPreview(undefined); setValidation(""); }}>Edit terms</button>
            </div>
          </fieldset>
        </form>
      ) : (
        <>
          <label>Instructions<textarea rows={4} value={language} placeholder={exampleMandate} onChange={(event) => setLanguage(event.target.value)} /></label>
          <div className="bank-actions">
            <button type="button" className="bank-secondary" onClick={() => setLanguage(exampleMandate)}>Use example</button>
            <button type="button" className="bank-secondary" disabled={!language.trim()} onClick={parse}>Parse terms</button>
          </div>
          <form className="bank-form" onSubmit={review} aria-label="Editable mandate terms">
            <fieldset disabled={disabled}>
              <input type="hidden" name="account" value={accountId} />
              <label>Agent<select name="agent" value={draft.agent} onChange={(event) => edit("agent", event.target.value)}>
                <option value="">Choose an agent</option>
                {agents.map((agent) => <option key={agent.agent_id} value={agent.agent_id}>{agent.name}</option>)}
              </select></label>
              <label>Grant category<select name="category" value={draft.category} onChange={(event) => edit("category", event.target.value)}>
                <option value="">Any category</option>
                {categories.map((category) => <option key={category}>{category}</option>)}
              </select></label>
              <fieldset className="bank-payees">
                <legend>Payee allow-list</legend>
                {catalog.payees.map((payee) => <label className="bank-choice" key={payee.payee_id}>
                  <input type="checkbox" name="payee" value={payee.payee_id} checked={draft.payees.includes(payee.payee_id)}
                    onChange={(event) => edit("payees", event.target.checked ? [...draft.payees, payee.payee_id]
                      : draft.payees.filter((id) => id !== payee.payee_id))} />
                  {payee.display_name}
                </label>)}
              </fieldset>
              <div className="bank-fields">
                <label>Per-payment cap (₹)<input name="perTxn" inputMode="decimal" value={draft.perTxn} onChange={(event) => edit("perTxn", event.target.value)} /></label>
                <label>Total cap (₹)<input name="lifetime" inputMode="decimal" value={draft.lifetime} onChange={(event) => edit("lifetime", event.target.value)} /></label>
                <label>Payment count cap<input name="count" inputMode="numeric" value={draft.count} onChange={(event) => edit("count", event.target.value)} /></label>
                <label>Ask me above (₹)<input name="threshold" inputMode="decimal" value={draft.threshold} onChange={(event) => edit("threshold", event.target.value)} /></label>
              </div>
              <label>Expiry (IST)<input name="expiry" type="datetime-local" step="1" value={draft.expiry} onChange={(event) => edit("expiry", event.target.value)} /></label>
              <label className="bank-choice"><input name="newPayee" type="checkbox" checked={draft.newPayee}
                onChange={(event) => edit("newPayee", event.target.checked)} />Ask me the first time a payee is paid</label>
              <button type="submit">Review mandate</button>
            </fieldset>
          </form>
        </>
      )}
    </section>
  );
}

export function BankMandates({ overview, disabled, mutate }: { overview: Overview; disabled: boolean; mutate: Mutation }) {
  const [composing, setComposing] = useState(false);
  const [revoking, setRevoking] = useState<string>();
  const { catalog } = overview;
  return (
    <div className="bank-stack">
      <div className="bank-section-title"><h2>Agents &amp; mandates</h2>
        <button className="bank-secondary" type="button" onClick={() => setComposing(!composing)}>
          {composing ? "Close composer" : "Write a mandate"}</button>
      </div>
      {composing && <MandateComposer overview={overview} disabled={disabled} mutate={mutate} close={() => setComposing(false)} />}
      {!overview.mandates.length && <p className="bank-card bank-muted">No mandates for this account.</p>}
      {overview.mandates.map((item) => {
        const { terms, usage } = item;
        const id = terms.mandate_id;
        const status = item.status === "ACTIVE" && Date.parse(terms.valid_until) <= Date.now() ? "EXPIRED" : item.status;
        const { hash, keyId, signature } = signingMetadata(item);
        const stepUp = getStepUp(terms);
        return (
          <article key={id} className="bank-card bank-mandate" data-mandate-id={id}>
            <div className="bank-section-title">
              <h3>{catalog.agents.find((agent) => agent.agent_id === terms.agent_id)?.name ?? terms.agent_id}</h3>
              <span className="bank-tag" data-state={status}>{status[0] + status.slice(1).toLowerCase()}</span>
            </div>
            <p className="bank-muted">Expires {formatKolkataTimestamp(terms.valid_until)}</p>
            {terms.grants.map((grant) => {
              const used = usage.find(({ grant_id }) => grant_id === grant.grant_id);
              const cap = amountField(grant, "max_total_paise");
              const countCap = amountField(grant, "max_count");
              return (
                <div className="bank-grant" key={grant.grant_id}>
                  <strong>{actionLabels[grant.action]}</strong>
                  {used && <>
                    <p>{formatInrPaise(used.committed_paise)} used{cap !== undefined && ` of ${formatInrPaise(cap)}`}
                      {used.reserved_paise > 0 && ` · ${formatInrPaise(used.reserved_paise)} reserved`}</p>
                    {cap !== undefined && cap > 0 && <progress aria-label="Mandate budget used" max={cap} value={used.committed_paise} />}
                    {used.remaining_count !== null && <p>{used.remaining_count} {used.remaining_count === 1 ? "use" : "uses"} left</p>}
                    {used.remaining_count !== null && countCap !== undefined && countCap > 0
                      && <progress aria-label="Mandate uses remaining" max={countCap} value={used.remaining_count} />}
                  </>}
                  <details>
                    <summary>Grant terms</summary>
                    <TermList catalog={catalog} grant={grant} expiry={terms.valid_until} stepUp={stepUp} />
                  </details>
                </div>
              );
            })}
            {(hash || keyId || signature) && (
              <details className="bank-signature">
                <summary>Signing details</summary>
                <dl className="bank-terms">
                  {hash && <div><dt>Mandate hash</dt><dd><code>{hash}</code></dd></div>}
                  {keyId && <div><dt>Signing key ID</dt><dd><code>{keyId}</code></dd></div>}
                  {signature && <div><dt>Signature preview</dt><dd><code>{signature.length > 28 ? `${signature.slice(0, 28)}…` : signature}</code></dd></div>}
                </dl>
              </details>
            )}
            {status === "ACTIVE" && (revoking === id ? (
              <div className="bank-revoke">
                <p>Revoking cancels pending approvals under this mandate.</p>
                <div className="bank-actions">
                  <button type="button" className="bank-danger" disabled={disabled} onClick={() => void mutate(async (signal) => {
                    await bankApi.revoke(id, signal);
                    setRevoking(undefined);
                    return "Mandate revoked.";
                  })}>Confirm revoke</button>
                  <button type="button" className="bank-secondary" onClick={() => setRevoking(undefined)}>Keep mandate</button>
                </div>
              </div>
            ) : <button type="button" className="bank-danger" disabled={disabled} onClick={() => setRevoking(id)}>Revoke</button>)}
          </article>
        );
      })}
    </div>
  );
}
