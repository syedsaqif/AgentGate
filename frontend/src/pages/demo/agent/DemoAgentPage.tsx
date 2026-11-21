import { useEffect, useRef, useState, type FormEvent } from "react";
import { isFixture, readCatalogue, softReset, startRun, streamRun, type Catalogue, type Scenario, type StartInput, type Step } from "./agentApi";
import { AgentResult } from "./agentResult";
import { Fixture, TestLab } from "./TestLab";
import "./demoAgent.css";

type Run = { id: string; prompt: string; attachment?: string; scenario?: string; steps: Step[] };
const storageKey = "agentgate-demo-runs";
function readRuns(): Run[] {
  try {
    const saved = JSON.parse(sessionStorage.getItem(storageKey) ?? "[]");
    if (Array.isArray(saved)) return saved.filter((run) => typeof run?.id === "string" && Array.isArray(run.steps));
  } catch { /* Session history is optional. */ }
  return [];
}

export default function DemoAgentPage() {
  const [catalogue, setCatalogue] = useState<Catalogue>();
  const [catalogueFailed, setCatalogueFailed] = useState(false);
  const [catalogueAttempt, setCatalogueAttempt] = useState(0);
  const [runs, setRuns] = useState(readRuns);
  const [selected, setSelected] = useState(() => new URLSearchParams(window.location.search).get("run_id") ?? "");
  const [fixture, setFixture] = useState<Scenario>();
  const [prompt, setPrompt] = useState("");
  const [attachment, setAttachment] = useState<string>();
  const [preset, setPreset] = useState<Scenario>();
  const [cautious, setCautious] = useState(false);
  const [sheet, setSheet] = useState(false);
  const [busy, setBusy] = useState(false);
  const [feedback, setFeedback] = useState("");
  const [error, setError] = useState("");
  const [connection, setConnection] = useState("");
  const action = useRef<AbortController | null>(null);
  const records = useRef(runs);
  records.current = runs;
  const labButton = useRef<HTMLButtonElement>(null);
  const input = useRef<HTMLTextAreaElement>(null);
  const returnToComposer = useRef(false);
  const active = runs.find(({ id }) => id === selected);

  useEffect(() => {
    const controller = new AbortController();
    void readCatalogue(controller.signal).then((data) => {
      if (!controller.signal.aborted) { setCatalogue(data); setCatalogueFailed(false); }
    }).catch(() => { if (!controller.signal.aborted) setCatalogueFailed(true); });
    return () => controller.abort();
  }, [catalogueAttempt]);
  useEffect(() => () => action.current?.abort(), []);
  useEffect(() => {
    try { sessionStorage.setItem(storageKey, JSON.stringify(runs)); } catch { /* Storage may be restricted. */ }
  }, [runs]);

  function select(id: string) {
    const url = new URL(window.location.href);
    if (id) url.searchParams.set("run_id", id);
    else url.searchParams.delete("run_id");
    window.history.replaceState(null, "", url);
    setSelected(id);
    setFixture(undefined);
  }

  useEffect(() => {
    setConnection("");
    if (!selected) return;
    if (!/^run_[\w-]+$/.test(selected)) {
      setConnection("This run link is invalid. Select a chat from history or start a new chat.");
      return;
    }
    let cursor = records.current.find(({ id }) => id === selected)?.steps.at(-1)?.seq ?? 0;
    if (records.current.find(({ id }) => id === selected)?.steps.at(-1)?.name === "completed") return;
    const controller = new AbortController();
    let timer: number | undefined;
    let finished = false;

    async function connect() {
      try {
        await streamRun(selected, cursor, controller.signal, (step) => {
          if (controller.signal.aborted || step.seq <= cursor) return;
          cursor = step.seq;
          finished = step.name === "completed";
          setConnection("");
          setRuns((previous) => {
            const existing = previous.find(({ id }) => id === selected);
            if (!existing) return [{ id: selected, prompt: "Saved run", steps: [step] }, ...previous].slice(0, 20);
            if (existing.steps.some(({ seq }) => seq === step.seq)) return previous;
            return previous.map((run) => run.id === selected ? { ...run, steps: [...run.steps, step] } : run);
          });
        });
      } catch (failure) {
        if (controller.signal.aborted) return;
        if (failure && typeof failure === "object" && "status" in failure && failure.status === 404) {
          setConnection("This run is no longer available on the demo server.");
          return;
        }
      }
      if (controller.signal.aborted || finished) return;
      setConnection("Reconnecting to the existing run…");
      timer = window.setTimeout(() => void connect(), 750);
    }

    void connect();
    return () => { controller.abort(); window.clearTimeout(timer); };
  }, [selected]);

  function usePrompt(card: Scenario) {
    setPrompt(card.prompt);
    setAttachment(card.attachment ?? undefined);
    setPreset(card);
    returnToComposer.current = sheet;
    if (!sheet) input.current?.focus();
  }

  async function run(payload: StartInput, display: string, document?: string) {
    if (action.current) return;
    const controller = new AbortController();
    action.current = controller;
    setError(""); setFeedback(""); setBusy(true);
    try {
      const id = await startRun({ ...payload, agent_mode: cautious ? "cautious" : "naive" }, controller.signal);
      if (controller.signal.aborted) return;
      setRuns((previous) => [{ id, prompt: display, attachment: document, scenario: payload.scenario_id, steps: [] }, ...previous].slice(0, 20));
      select(id);
      setPrompt(""); setAttachment(undefined); setPreset(undefined);
    } catch {
      if (!controller.signal.aborted) setError("The server did not confirm a new run. Check Demo Bank before submitting again.");
    } finally {
      action.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  function submit(event: FormEvent<HTMLFormElement>) {
    event.preventDefault();
    if (!prompt.trim()) return;
    const id = preset && prompt.trim() === preset.prompt.trim() && (attachment ?? "") === (preset.attachment ?? "")
      ? preset.id : undefined;
    void run({ prompt: prompt.trim(), attachment, scenario_id: id }, prompt.trim(), attachment);
  }

  async function reset() {
    if (action.current) return;
    const controller = new AbortController();
    action.current = controller;
    setBusy(true); setError(""); setFeedback("");
    try {
      const { message } = await softReset(controller.signal);
      if (!controller.signal.aborted) setFeedback(message);
    } catch {
      if (!controller.signal.aborted) setError("The demo server did not confirm the reset.");
    } finally {
      action.current = null;
      if (!controller.signal.aborted) setBusy(false);
    }
  }

  const cards = catalogue?.items ?? [];
  const chips = ["P1", "P4", "P8"].flatMap((id) => cards.filter((card) => card.id === id && !isFixture(card)));
  const selectedCard = cards.find((card) => card.id === active?.scenario);
  return <main className="agent-demo">
    <aside className="agent-history">
      <h1>Payment assistant</h1>
      <button type="button" onClick={() => { select(""); setPrompt(""); setPreset(undefined); setAttachment(undefined); }}>New chat</button>
      {!!runs.length && <nav aria-label="Chat history">{runs.map((record) =>
        <button key={record.id} type="button" aria-current={selected === record.id ? "page" : undefined}
          title={record.prompt} onClick={() => select(record.id)}>{record.prompt}</button>)}</nav>}
    </aside>
    <div className="agent-workspace">
      <div className="agent-conversation">
        {error && <p role="alert" className="agent-alert">{error}</p>}
        {feedback && <p role="status">{feedback}</p>}
        {fixture ? <Fixture card={fixture} /> : active ? <>
          <p className="agent-user">{active.prompt}</p>
          {active.attachment && <details open className="agent-card"><summary>Attachment</summary>
            <p className="agent-document">{active.attachment}</p></details>}
          <AgentResult steps={active.steps} card={selectedCard} />
          {!active.steps.length && <p role="status">Waiting for scripted agent steps…</p>}
        </> : !selected && <><h2>What would you like to pay?</h2>
          <div className="agent-chips">{chips.map((card) =>
            <button key={card.id} type="button" onClick={() => usePrompt(card)}>{card.title}</button>)}</div>
        </>}
        {connection && <p role="status">{connection}</p>}
      </div>
      <form className="agent-composer" onSubmit={submit}>
        <label htmlFor="agent-prompt">Message</label>
        <textarea id="agent-prompt" ref={input} rows={3} placeholder="Describe a demo payment" value={prompt}
          maxLength={1000} onChange={(event) => setPrompt(event.target.value)} />
        {attachment && <div className="agent-attached">
          <details><summary>Attachment preview</summary><p className="agent-document">{attachment}</p></details>
          <button type="button" onClick={() => setAttachment(undefined)}>Remove attachment</button>
        </div>}
        <div className="agent-actions">
          <button type="submit" disabled={busy || !prompt.trim()}>Send</button>
          <button type="button" ref={labButton} disabled={!catalogue} onClick={() => setSheet(true)}>Test lab</button>
          <label className="agent-mode"><input type="checkbox" checked={cautious}
            onChange={(event) => setCautious(event.target.checked)} />Cautious agent</label>
        </div>
        {catalogueFailed && <p>The scenario catalogue is unavailable. <button type="button"
          onClick={() => setCatalogueAttempt((attempt) => attempt + 1)}>Retry catalogue</button></p>}
      </form>
    </div>
    {sheet && catalogue && <TestLab catalogue={catalogue} disabled={busy}
      onClose={() => {
        setSheet(false);
        (returnToComposer.current ? input.current : labButton.current)?.focus();
        returnToComposer.current = false;
      }} usePrompt={usePrompt} openFixture={(card) => { select(""); setFixture(card); }}
      runCard={(card) => { if (!isFixture(card)) void run({ scenario_id: card.id }, card.prompt, card.attachment ?? undefined); }}
      run={(...args) => void run(...args)} reset={() => void reset()}
      latest={(id) => runs.find((record) => record.scenario === id)?.steps}
    />}
  </main>;
}
