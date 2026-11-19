import { useRef, useState, type FormEvent } from "react";
import { isMoneyAction, mandateActions, type MandateAction, type MandateTermsInput } from "../../api/mandateWire";
import type { CustomerBootstrap } from "../../api/shellWire";
import { formatInrPaise, formatKolkataTimestamp } from "../../formatting";
import { actionLabels, maskedAccount, readMandateDraft, weekdays } from "./mandateDraft";

type MandateFormProps = {
  bootstrap: CustomerBootstrap;
  disabled: boolean;
  create: (terms: MandateTermsInput, pin: string) => Promise<boolean>;
};

export function MandateForm({ bootstrap, disabled, create }: MandateFormProps) {
  const [agentId, setAgentId] = useState("");
  const [actions, setActions] = useState<MandateAction[]>([]);
  const [preview, setPreview] = useState<MandateTermsInput | null>(null);
  const [validation, setValidation] = useState("");
  const draft = useRef<HTMLFormElement>(null);
  const confirming = useRef(false);
  const agent = bootstrap.agents.find((card) => card.agent_id === agentId);
  const allowed = mandateActions.filter((action) => agent?.max_scopes?.includes(action));
  const money = actions.some(isMoneyAction);

  function review(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const result = readMandateDraft(new FormData(event.currentTarget), bootstrap, actions);
    setValidation(result.ok ? "" : result.error);
    if (result.ok) setPreview(result.value);
  }

  async function confirm(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!preview || disabled || confirming.current) return;
    const pin = String(new FormData(event.currentTarget).get("pin") ?? "");
    event.currentTarget.reset();
    if (!pin.trim()) {
      setValidation("Enter your Customer PIN.");
      return;
    }
    if (Date.parse(preview.valid_until) <= Date.now()) {
      setValidation("The reviewed expiry has passed. Edit the terms and choose a future expiry.");
      return;
    }
    setValidation("");
    confirming.current = true;
    try {
      if (await create(preview, pin)) {
        setPreview(null);
        setAgentId("");
        setActions([]);
        draft.current?.reset();
      }
    } finally {
      confirming.current = false;
    }
  }

  return (
    <section className="panel mandate-form" aria-labelledby="create-mandate">
      <h2 id="create-mandate">Create mandate</h2>
      {validation && <p role="alert" className="notice notice-error">{validation}</p>}
      {(!bootstrap.accounts.length || !bootstrap.agents.length) ? (
        <p>An account and an available agent are required to create a mandate.</p>
      ) : (
        <form ref={draft} hidden={preview !== null} onSubmit={review}>
          <fieldset disabled={disabled}>
            <div className="mandate-fields">
              <label>Account<select name="account" defaultValue="">
                <option value="">Select an account</option>
                {bootstrap.accounts.map((account) => (
                  <option key={account.account_id} value={account.account_id}>{maskedAccount(account)}</option>
                ))}
              </select></label>
              <label>Agent<select name="agent" value={agentId} onChange={(event) => {
                setAgentId(event.target.value);
                setActions([]);
              }}>
                <option value="">Select an agent</option>
                {bootstrap.agents.map((card) => (
                  <option key={card.agent_id} value={card.agent_id}>{card.name} ({card.vendor})</option>
                ))}
              </select></label>
            </div>
            {agent && <p>Key fingerprint <code>{agent.jkt || "Unavailable"}</code></p>}
            {agent && (allowed.length ? (
              <fieldset className="mandate-choices">
                <legend>Allowed actions</legend>
                {allowed.map((action) => (
                  <label key={action}><input type="checkbox" checked={actions.includes(action)}
                    onChange={(event) => setActions(event.target.checked
                      ? [...actions, action] : actions.filter((selected) => selected !== action))} />
                  {actionLabels[action]}</label>
                ))}
              </fieldset>
            ) : <p>The gateway supplied no supported actions for this agent.</p>)}
            {money && (
              <>
                <fieldset className="mandate-choices">
                  <legend>Allowed payees</legend>
                  {bootstrap.payees.map((payee) => (
                    <label key={payee.payee_id}>
                      <input type="checkbox" name="payee" value={payee.payee_id} />{payee.display_name}
                    </label>
                  ))}
                  {!bootstrap.payees.length && <p>No payees available.</p>}
                </fieldset>
                <div className="mandate-fields">
                  <label>Per-transaction cap (INR)<input name="perTxn" inputMode="decimal" /></label>
                  <label>Mandate-lifetime cap (INR)<input name="lifetime" inputMode="decimal" /></label>
                </div>
              </>
            )}
            <div className="mandate-fields">
              <label>Count limit per grant<input name="count" inputMode="numeric" /></label>
              <label>Expiry (IST)<input type="datetime-local" name="expiry" /></label>
            </div>
            <details>
              <summary>Optional time and approval terms</summary>
              <fieldset className="mandate-choices">
                <legend>Allowed weekdays (IST)</legend>
                {weekdays.map((day, index) => (
                  <label key={day}><input type="checkbox" name="weekday" value={index + 1} />{day}</label>
                ))}
              </fieldset>
              <div className="mandate-fields">
                <label>Window starts (IST)<input type="time" name="start" /></label>
                <label>Window ends (IST)<input type="time" name="end" /></label>
              </div>
              <fieldset className="mandate-choices">
                <legend>Step-up approval</legend>
                <label><input type="checkbox" name="newPayee" defaultChecked />Require approval for a new allowed payee</label>
                {actions.map((action) => (
                  <label key={action}><input type="checkbox" name="alwaysAsk" value={action} />Always ask to {actionLabels[action].toLowerCase()}</label>
                ))}
              </fieldset>
              {money && <label>Approval threshold (INR)<input name="threshold" inputMode="decimal" /></label>}
            </details>
            {actions.length > 1 && <p>Each selected action has its own grant and limits.</p>}
            <button type="submit">Review mandate</button>
          </fieldset>
        </form>
      )}
      {preview && (
        <form method="post" autoComplete="off" onSubmit={confirm} aria-labelledby="mandate-review-title">
          <fieldset disabled={disabled}>
            <MandatePreview terms={preview} bootstrap={bootstrap} />
            <label>Confirmation PIN<input name="pin" type="password" inputMode="numeric" required /></label>
            <div className="mandate-buttons">
              <button type="submit">Confirm mandate</button>
              <button type="button" className="secondary" onClick={() => {
                setPreview(null);
                setValidation("");
              }}>Edit terms</button>
            </div>
          </fieldset>
        </form>
      )}
    </section>
  );
}

function MandatePreview({ terms, bootstrap }: { terms: MandateTermsInput; bootstrap: CustomerBootstrap }) {
  const agent = bootstrap.agents.find((card) => card.agent_id === terms.agent_id);
  const stepUp = terms.step_up;
  return (
    <div>
      <h3 id="mandate-review-title">Review grant terms</h3>
      <p>{agent?.name} ({agent?.vendor}) · Key fingerprint <code>{agent?.jkt || "Unavailable"}</code></p>
      <p>Starts <time dateTime={terms.valid_from}>{formatKolkataTimestamp(terms.valid_from)}</time> · Balanced risk profile</p>
      <p>Expires <time dateTime={terms.valid_until}>{formatKolkataTimestamp(terms.valid_until)}</time></p>
      <ul>
        {terms.grants.map((grant) => (
          <li key={grant.action}>
            <strong>{actionLabels[grant.action]}</strong>
            <div>Account {bootstrap.accounts.filter((card) => grant.account_ids.includes(card.account_id)).map(maskedAccount).join(", ")}</div>
            {grant.payee_ids && <div>Payees {bootstrap.payees.filter((payee) => grant.payee_ids?.includes(payee.payee_id))
              .map((payee) => payee.display_name).join(", ")}</div>}
            {grant.max_per_txn_paise !== undefined && <div>Per transaction {formatInrPaise(grant.max_per_txn_paise)}</div>}
            {grant.max_total_paise !== undefined && <div>Lifetime cap {formatInrPaise(grant.max_total_paise)}</div>}
            <div>Up to {grant.max_count} uses over the lifetime of this mandate</div>
            {grant.time_window && <div>{grant.time_window.weekdays.map((day) => weekdays[day - 1]).join(", ")}
              {" "}{grant.time_window.start_local}–{grant.time_window.end_local} IST</div>}
          </li>
        ))}
      </ul>
      <p>New allowed payee approval {stepUp.new_payee_requires_approval ? "required" : "off"}</p>
      {stepUp.amount_above_paise !== undefined && <p>Approval above {formatInrPaise(stepUp.amount_above_paise)}</p>}
      {stepUp.always_ask_actions.length > 0 && <p>Always ask for {stepUp.always_ask_actions.map((action) => actionLabels[action]).join(", ")}</p>}
    </div>
  );
}
