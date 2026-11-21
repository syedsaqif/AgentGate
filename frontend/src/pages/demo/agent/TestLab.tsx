import { useEffect, useRef, useState, type FormEvent } from "react";
import { parseRupeePaise } from "../../../formatting";
import { isFixture, type Catalogue, type Scenario, type StartInput, type Step } from "./agentApi";
import { Comparison } from "./agentResult";

const groups = [
  ["", "All"], ["prompt_injection", "Prompt injection"], ["limits_and_abuse", "Limits and abuse"],
  ["identity_integrity", "Identity and request integrity"], ["audit_tamper", "Audit tamper"],
] as const;

export function Fixture({ card }: { card: Scenario }) {
  const fixture = card.fixture;
  return <section className="agent-result" aria-label="Fixture result">
    <p className="agent-tag">Recorded fixture · Read-only</p>
    <h2>{card.title}</h2>
    <p>No payment request is dispatched.</p>
    {typeof fixture?.message === "string" && <p>{fixture.message}</p>}
    <dl>
      {typeof fixture?.stage === "string" && <><dt>Recorded stop</dt><dd>{fixture.stage}</dd></>}
      {typeof fixture?.failure_stage === "string" && <><dt>Recorded stop</dt><dd>{fixture.failure_stage}</dd></>}
      {typeof fixture?.http_status === "number" && <><dt>Recorded response</dt><dd>HTTP {fixture.http_status}</dd></>}
      {typeof fixture?.reason_code === "string" && <><dt>Reason code</dt><dd><code>{fixture.reason_code}</code></dd></>}
      {typeof fixture?.source_test === "string" && <><dt>Fixture source</dt><dd><code>{fixture.source_test}</code></dd></>}
    </dl>
  </section>;
}

function CustomPolicy({ catalogue, disabled, run, openFixture }: {
  catalogue: Catalogue; disabled: boolean;
  run: (payload: StartInput, prompt: string, attachment?: string) => void; openFixture: (card: Scenario) => void;
}) {
  const [error, setError] = useState("");
  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    const form = new FormData(event.currentTarget);
    const amount = parseRupeePaise(String(form.get("amount") ?? ""));
    if (!amount.ok || amount.value < 1) {
      setError(amount.ok ? "Enter an amount greater than zero." : amount.error);
      return;
    }
    setError("");
    const action = String(form.get("action"));
    const payee_id = String(form.get("payee"));
    const document = String(form.get("document") || "") || undefined;
    run({ overrides: { action, payee_id, amount_paise: amount.value, category: String(form.get("category")), document } },
      `${action === "transfer" ? "Transfer" : "Pay"} ₹${String(form.get("amount"))} to ${payee_id}`,
      catalogue.items.find((card) => card.id === document)?.attachment ?? undefined);
  }
  return <details className="agent-custom">
    <summary>Custom policy test</summary>
    <form onSubmit={submit}>
      <label>Action<select name="action"><option value="pay_bill">Pay bill</option><option value="transfer">Transfer</option></select></label>
      <label>Payee<select name="payee">{catalogue.policy_inputs.payees.map((payee) =>
        <option value={payee.id} key={payee.id}>{payee.name} · {payee.id}</option>)}</select></label>
      <label>Amount (₹)<input name="amount" inputMode="decimal" required /></label>
      <label>Declared category<select name="category" defaultValue={catalogue.policy_inputs.payees[0]?.category}>
        {catalogue.policy_inputs.categories.map((category) => <option key={category}>{category}</option>)}</select></label>
      <label>Document source<select name="document"><option value="">No attachment</option>
        {catalogue.items.filter((card) => !isFixture(card) && card.attachment).map((card) =>
          <option value={card.id} key={card.id}>{card.title}</option>)}</select></label>
      {error && <p role="alert">{error}</p>}
      <button disabled={disabled} type="submit">Run custom policy test</button>
    </form>
    <label>Identity or integrity fixture<select value="" onChange={(event) => {
      const card = catalogue.items.find(({ id }) => id === event.target.value);
      if (card) openFixture(card);
    }}>
      <option value="">Choose a read-only demonstration</option>
      {catalogue.items.filter(({ group }) => group === "identity_integrity").map((card) =>
        <option value={card.id} key={card.id}>{card.title}</option>)}
    </select></label>
  </details>;
}

export function TestLab({ catalogue, disabled, onClose, usePrompt, runCard, openFixture, run, latest, reset }: {
  catalogue: Catalogue; disabled: boolean; onClose: () => void; usePrompt: (card: Scenario) => void;
  runCard: (card: Scenario) => void; openFixture: (card: Scenario) => void;
  run: (payload: StartInput, prompt: string, attachment?: string) => void;
  latest: (id: string) => Step[] | undefined;
  reset: () => void;
}) {
  const dialog = useRef<HTMLDialogElement>(null);
  const [tab, setTab] = useState<"normal" | "attack">("normal");
  const [group, setGroup] = useState("");
  const normalTab = useRef<HTMLButtonElement>(null);
  const attackTab = useRef<HTMLButtonElement>(null);
  useEffect(() => { if (dialog.current && !dialog.current.open) dialog.current.showModal(); }, []);
  const close = () => dialog.current?.close();
  const cards = catalogue.items.filter((card) => card.kind === tab && (tab === "normal" || !group || group === card.group));
  return <dialog ref={dialog} className="agent-sheet" aria-labelledby="agent-lab-title" onClose={onClose}>
    <header><h2 id="agent-lab-title">Test lab</h2><button type="button" autoFocus onClick={close}>Close</button></header>
    <div className="agent-tabs" role="tablist" aria-label="Scenario types" onKeyDown={(event) => {
      if (!["ArrowRight", "ArrowLeft", "Home", "End"].includes(event.key)) return;
      event.preventDefault();
      const next = event.key === "Home" ? "normal" : event.key === "End" ? "attack" : tab === "normal" ? "attack" : "normal";
      setTab(next);
      (next === "normal" ? normalTab : attackTab).current?.focus();
    }}>
      <button id="agent-normal-tab" ref={normalTab} role="tab" type="button" aria-controls="agent-cards" aria-selected={tab === "normal"}
        tabIndex={tab === "normal" ? 0 : -1} onClick={() => setTab("normal")}>Works normally</button>
      <button id="agent-attack-tab" ref={attackTab} role="tab" type="button" aria-controls="agent-cards" aria-selected={tab === "attack"}
        tabIndex={tab === "attack" ? 0 : -1} onClick={() => setTab("attack")}>Attacks</button>
    </div>
    <CustomPolicy catalogue={catalogue} disabled={disabled} run={(...args) => { run(...args); close(); }}
      openFixture={(card) => { openFixture(card); close(); }} />
    <details className="agent-custom">
      <summary>Soft reset</summary>
      <p>Reinstates BillPal, replaces the active demo mandate, signs out Demo Bank and clears the retry cache. Existing balances, ledger entries and risk history stay as they are.</p>
      <button type="button" disabled={disabled} onClick={() => { reset(); close(); }}>Apply soft reset</button>
    </details>
    <div role="tabpanel" id="agent-cards" aria-labelledby={tab === "normal" ? "agent-normal-tab" : "agent-attack-tab"}>
      {tab === "attack" && <div className="agent-chips" aria-label="Attack groups">
        {groups.map(([key, name]) => <button key={key} type="button" aria-pressed={group === key} onClick={() => setGroup(key)}>{name}</button>)}
      </div>}
      {cards.map((card) => {
        const fixture = isFixture(card);
        const observed = latest(card.id);
        return <article className="agent-card" key={card.id}>
          <h3>{card.title}</h3>
          {!fixture && <p>{card.description}</p>}
          <blockquote>{card.prompt}</blockquote>
          {card.attachment && <details><summary>Attachment preview</summary><p className="agent-document">{card.attachment}</p></details>}
          <p><strong>Prerequisite</strong> {card.prerequisites}</p>
          <p className="agent-tag">{fixture ? "Recorded fixture" : card.observation_available && card.observed_result
            ? "Catalogue expectation · baseline available" : "Unmeasured expectation"}</p>
          <p><strong>Expected</strong> {card.expected_verdict} · {card.expected_stop_point}</p>
          {fixture ? <button type="button" onClick={() => { openFixture(card); close(); }}>View fixture</button>
            : <div className="agent-actions">
              <button type="button" onClick={() => { usePrompt(card); close(); }}>Use prompt</button>
              <button type="button" disabled={disabled} onClick={() => { runCard(card); close(); }}>Run</button>
            </div>}
          {observed && <Comparison card={card} steps={observed} />}
        </article>;
      })}
    </div>
  </dialog>;
}
