import { useEffect, useMemo, useRef, useState } from "react";
import { Playback } from "../DemoGatePage";
import { buildPlayback, projectGate } from "../gateTimeline";
import { formatInrPaise } from "../../../formatting";
import { useGateStream } from "../useGateStream";
import { AgentResult, summarize } from "../agent/agentResult";
import { Fixture } from "../agent/TestLab";
import { isFixture, readCatalogue, softReset, startRun, streamRun, type Scenario, type Step } from "../agent/agentApi";
import DemoBankPage from "../bank/DemoBankPage";
import { BankHome } from "../bank/BankHome";
import { appendStep, archive, replayFrames, type Capture } from "./recordings";
import "../agent/demoAgent.css";
import "./stage.css";

type Mode = "live" | "recorded";
type Speed = 0.5 | 1 | 2 | 4 | 10;
type Run = { key: string; id: string };
const initial = () => new URLSearchParams(window.location.search);
const aliases = (card: Scenario) => (card as Capture["card"]).aliases ?? [];
const title = (card: Scenario) => [...aliases(card), card.id].join(" / ") + " · " + card.title;

const SCENARIO_GROUPS = [
  { id: "normal", label: "Normal Payments & Step-Up Approvals (P1–P8, N2)" },
  { id: "prompt_injection", label: "Prompt Injection & Jailbreaks (I1–I5)" },
  { id: "limits_and_abuse", label: "Policy & Spending Limit Abuse (L1–L7)" },
  { id: "identity_integrity", label: "Cryptographic & Identity Attacks (C1–C6)" },
  { id: "audit_tamper", label: "Audit Log Tamper Detection (T1)" },
] as const;

function getScenarioGroup(card?: Scenario): string {
  if (card?.group) return card.group;
  const id = card?.id ?? "";
  if (id.startsWith("P") || id.startsWith("N")) return "normal";
  if (id.startsWith("I")) return "prompt_injection";
  if (id.startsWith("L")) return "limits_and_abuse";
  if (id.startsWith("C")) return "identity_integrity";
  if (id.startsWith("T")) return "audit_tamper";
  return "other";
}

function useAgentHistory(run?: Run) {
  const [steps, setSteps] = useState<Step[]>([]);
  const [status, setStatus] = useState("");
  useEffect(() => {
    setSteps([]); setStatus("");
    if (!run) return;
    const controller = new AbortController();
    let cursor = 0, finished = false;
    let retry: number | undefined;
    async function connect() {
      try {
        await streamRun(run!.id, cursor, controller.signal, step => {
          if (controller.signal.aborted || step.seq <= cursor) return;
          cursor = step.seq;
          finished = step.name === "completed";
          setStatus("");
          setSteps(rows => appendStep(rows, step));
        });
      } catch (error) {
        if (controller.signal.aborted) return;
        if (error && typeof error === "object" && "status" in error && error.status === 404) {
          setStatus("Run history expired. Choose another run."); return;
        }
      }
      if (!finished && !controller.signal.aborted) {
        setStatus("Reconnecting to this run…");
        retry = window.setTimeout(() => void connect(), 800);
      }
    }
    void connect();
    return () => { controller.abort(); window.clearTimeout(retry); };
  }, [run]);
  return {steps, status};
}

function RecordedBank({frame}: {frame?: ReturnType<typeof replayFrames>[number]}) {
  return <section className="bank-demo">
    <header className="bank-header"><div><h2>Demo Bank</h2><p>Recorded account activity</p></div></header>
    {frame?.bank ? <BankHome overview={frame.bank} recordedAt={frame.at} notices={frame.notices} disabled
      mutate={async () => false} /> : <p>No account snapshot in this fixture.</p>}
  </section>;
}

function CompareRunSummary({ capture }: { capture?: Capture }) {
  if (!capture) return <p>No recording available.</p>;
  const { card } = capture;
  const frames = replayFrames(capture);
  const last = frames.at(-1);
  const result = summarize(last?.steps ?? []);
  const beats = last?.request && buildPlayback(last.request);
  const gate = last?.request && beats && projectGate(last.request, beats, beats.length);
  const start = frames[0]?.bank?.balance;
  const end = capture.phases?.length ? last?.bank?.balance : undefined;
  const delta = start !== undefined && end !== undefined ? end - start : undefined;
  const agentStatus = result.end?.agentStatus;
  const verdict = result.verdict ?? gate?.decision;
  const reason = result.reason ?? gate?.reason;

  return (
    <div className="compare-details">
      <h3>{card.title} ({card.id})</h3>
      <p className="compare-prompt">&ldquo;{card.prompt}&rdquo;</p>
      <dl className="compare-dl">
        <dt>{verdict ? "Recorded verdict" : "Expected verdict"}</dt>
        <dd>
          <strong className={`gate-verdict-badge verdict-${(verdict ?? card.expected_verdict).toLowerCase()}`}>
            {verdict ?? card.expected_verdict}
          </strong>
        </dd>
        <dt>Expected stop point</dt>
        <dd>{card.expected_stop_point}</dd>
        <dt>Risk Score</dt>
        <dd>{gate?.risk ? `${gate.risk.score} / 100${gate.risk.band ? ` (${gate.risk.band})` : ""}` : "Not recorded"}</dd>
        <dt>Money Moved</dt>
        <dd>{delta === undefined ? "Not recorded" : delta === 0 ? "₹0 moved" :
          `${formatInrPaise(Math.abs(delta))} ${delta < 0 ? "debited" : "credited"}`}</dd>
        <dt>BillPal Status</dt>
        <dd>
          <span className={agentStatus === "SUSPENDED" ? "badge-suspended" : agentStatus === "ACTIVE" ? "badge-active" : undefined}>
            {agentStatus ?? "Not recorded"}
          </span>
        </dd>
        {(reason || card.expected_reason_code) && (
          <>
            <dt>{reason ? "Recorded reason code" : "Expected reason code"}</dt>
            <dd><code>{reason ?? card.expected_reason_code}</code></dd>
          </>
        )}
      </dl>
    </div>
  );
}

export default function PresenterStage() {
  const [mode, setMode] = useState<Mode>(() => initial().get("mode") === "recorded" ? "recorded" : "live");
  const [selected, setSelected] = useState(() => initial().get("scenario") || "P1");
  const [catalogue, setCatalogue] = useState<Scenario[]>([]);
  const [catalogueAttempt, setCatalogueAttempt] = useState(0);
  const [speed, setSpeed] = useState<Speed>(1);
  const [skip, setSkip] = useState(false);
  const [epoch, setEpoch] = useState(0);
  const [showCompare, setShowCompare] = useState(false);
  const [compareKeyA, setCompareKeyA] = useState("P1");
  const [compareKeyB, setCompareKeyB] = useState("I1");
  const [run, setRun] = useState<Run | undefined>(() => {
    const id = initial().get("run");
    return mode === "live" && id && /^run_[\w-]+$/.test(id) ? {id, key: selected} : undefined;
  });
  const [recordKey, setRecordKey] = useState<string>();
  const [recordIndex, setRecordIndex] = useState(0);
  const [requestChoice, setRequestChoice] = useState("");
  const [fixture, setFixture] = useState<Scenario>();
  const [busy, setBusy] = useState(false);
  const [message, setMessage] = useState("");
  const [catalogueError, setCatalogueError] = useState(false);
  const action = useRef<AbortController | null>(null);
  const recorded = mode === "recorded";
  const live = !recorded && !fixture;
  const {feed, connection} = useGateStream(live, epoch);
  const history = useAgentHistory(live ? run : undefined);
  const record = recordKey ? archive[recordKey] : undefined;
  const frames = useMemo(() => record ? replayFrames(record) : [], [record]);
  const position = Math.min(recordIndex, frames.length - 1);
  const frame = frames[position];
  const selectedCard = recorded ? archive[selected]?.card : catalogue.find(card => card.id === selected);
  const currentCard = fixture ?? (recorded ? record?.card : catalogue.find(card => card.id === run?.key));
  const steps = live ? history.steps : frame?.steps ?? [];
  const ids = [...new Set(steps.flatMap(s => s.traceRequestId ? [s.traceRequestId] : s.requestId ? [s.requestId] : []))];
  const request = live ? feed.requests.get(requestChoice || ids.at(-1) || "") : frame?.request;
  const outcome = summarize(steps);
  const suspendedP5 = live && run?.key === "P5" && !!outcome.end && outcome.reason === "E_AUTH_AGENT_SUSPENDED";
  const waiting = live && !!run && !steps.some(s=>s.name === "completed") && !history.status.startsWith("Run history expired");
  const options = recorded ? Object.entries(archive).map(([key, value]) => ({key, card: value.card}))
    : catalogue.map(card => ({key: card.id, card}));
  options.sort((a,b) => a.key.localeCompare(b.key, "en", {numeric:true}));

  const groupedOptions = useMemo(() => {
    const map = new Map<string, typeof options>();
    for (const opt of options) {
      const grp = getScenarioGroup(opt.card);
      const list = map.get(grp) ?? [];
      list.push(opt);
      map.set(grp, list);
    }
    return map;
  }, [options]);

  useEffect(() => {
    if (recorded) return;
    const controller = new AbortController();
    setCatalogueError(false);
    void readCatalogue(controller.signal).then(result => {
      if (!controller.signal.aborted) setCatalogue(result.items);
    }).catch(() => { if (!controller.signal.aborted) setCatalogueError(true); });
    return () => controller.abort();
  }, [recorded, catalogueAttempt]);
  useEffect(() => () => action.current?.abort(), []);
  useEffect(() => {
    if (!recorded || !frame || position === frames.length - 1) return;
    if (skip || speed >= 10 || window.matchMedia?.("(prefers-reduced-motion: reduce)").matches) {
      setRecordIndex(frames.length - 1); return;
    }
    const timer = window.setTimeout(() => setRecordIndex(position + 1), frame.delay / speed);
    return () => window.clearTimeout(timer);
  }, [recorded, frame, position, frames, skip, speed]);

  function url(nextMode: Mode, key: string, id?: string) {
    const address = new URL(window.location.href);
    address.search = "";
    address.searchParams.set("mode", nextMode);
    address.searchParams.set("scenario", key);
    if (id) address.searchParams.set("run", id);
    window.history.replaceState(null, "", address);
  }
  function clear() {
    setRun(undefined); setRecordKey(undefined); setRecordIndex(0); setSkip(false);
    setFixture(undefined); setMessage(""); setRequestChoice("");
  }
  function changeMode(nextMode: Mode) {
    if (mode === nextMode) return;
    clear();
    const key = selected === "N2-after-N1" ? "N2" : selected;
    setMode(nextMode); setSelected(key); url(nextMode, key);
  }
  async function play() {
    if (!selectedCard || action.current || waiting) return;
    clear();
    if (isFixture(selectedCard)) { setFixture(selectedCard); return; }
    if (recorded) { setRecordKey(selected); return; }
    const controller = new AbortController();
    action.current = controller;
    setBusy(true);
    try {
      const id = await startRun({scenario_id: selectedCard.id}, controller.signal);
      if (!controller.signal.aborted) { setRun({key: selected, id}); url(mode, selected, id); }
    } catch {
      if (!controller.signal.aborted) setMessage("The server did not confirm a run. Check the bank before submitting again.");
    } finally {
      action.current = null; setBusy(false);
    }
  }
  async function reset(retryP5 = false) {
    if (action.current || waiting) return;
    if (recorded || fixture) { clear(); url(mode, selected); return; }
    const controller = new AbortController();
    action.current = controller;
    setBusy(true);
    try {
      const report = await softReset(controller.signal);
      if (controller.signal.aborted) return;
      setEpoch(n=>n+1);
      setMessage(report.message);
      if (retryP5 && !report.ready) return;
      clear();
      setMessage(report.message);
      url(mode, selected);
      if (retryP5) {
        try {
          const id = await startRun({scenario_id: "P5"}, controller.signal);
          if (!controller.signal.aborted) {
            setRun({key: "P5", id});
            url(mode, "P5", id);
            setMessage("Sign in to Demo Bank to review the new approval.");
          }
        } catch {
          setMessage("Reset completed, but the new run could not be confirmed. Check the bank before retrying.");
        }
      }
    } catch { setMessage("Soft reset could not be confirmed."); }
    finally { action.current = null; setBusy(false); }
  }
  return <div className="presenter-stage">
    <header className="stage-bar">
      <div className="stage-brand"><span>AgentGate</span><h1>Presenter stage</h1></div>
      <div className="stage-mode" role="group" aria-label="Data source">
        {(["live","recorded"] as const).map(value => <button key={value} aria-pressed={mode === value}
          disabled={busy} onClick={() => changeMode(value)}>{value === "live" ? "Live" : "Recorded"}</button>)}
      </div>
      <label className="stage-picker">Scenario<select aria-label="Scenario" value={selected} disabled={busy || (recorded && options.length === 0)}
        onChange={event => setSelected(event.target.value)}>
        {options.length === 0 && (
          <option value="" disabled>{recorded ? "No recordings found — switch to Live" :
            catalogueError ? "Scenario catalogue unavailable" : "Loading scenarios…"}</option>
        )}
        {SCENARIO_GROUPS.map(({ id: grpId, label }) => {
          const items = groupedOptions.get(grpId);
          if (!items || items.length === 0) return null;
          return (
            <optgroup key={grpId} label={label}>
              {items.map(({ key, card }) => (
                <option key={key} value={key}>
                  {title(card)}{key === "N2-after-N1" ? " · after N1" : ""}
                </option>
              ))}
            </optgroup>
          );
        })}
        {Array.from(groupedOptions.entries())
          .filter(([grpId]) => !SCENARIO_GROUPS.some(g => g.id === grpId))
          .map(([grpId, items]) => (
            <optgroup key={grpId} label={grpId}>
              {items.map(({ key, card }) => (
                <option key={key} value={key}>
                  {title(card)}{key === "N2-after-N1" ? " · after N1" : ""}
                </option>
              ))}
            </optgroup>
          ))}
      </select></label>
      <button className="stage-run" disabled={!selectedCard || busy || waiting} onClick={() => void play()}>
        {selectedCard && isFixture(selectedCard) ? "Show fixture" : recorded ? "Play recording" : "Run scenario"}</button>
      <label>Speed<select aria-label="Playback speed" value={speed} onChange={event => setSpeed(Number(event.target.value) as Speed)}>
        <option value={0.5}>0.5×</option>
        <option value={1}>1×</option>
        <option value={2}>2×</option>
        <option value={4}>4×</option>
        <option value={10}>Instant</option>
      </select></label>
      <button onClick={() => setSkip(true)} disabled={skip || (!request && !record)}>Skip to result</button>
      <button onClick={() => void reset()} disabled={busy || waiting}>{recorded || fixture ? "Clear playback" : "Soft reset"}</button>
      {recorded && (
        <button type="button" onClick={() => setShowCompare(true)}>
          Compare runs
        </button>
      )}
      <span className="stage-badge" data-mode={live ? "live" : "recorded"}>{live ? "Live" : fixture ? "Recorded fixture" : "Recorded"}</span>
    </header>
    <div className="stage-help">
      <span>{recorded ? (options.length === 0 ? "No offline recordings found. Switch to \"Live\" mode above to run real-time scenarios against the gateway." : "Read-only playback from local evidence.") : fixture ? "Read-only playback from local evidence." : "Soft reset reinstates BillPal, renews the mandate and signs the bank out. Balance, ledger and risk history carry over."}</span>
      {catalogueError && !recorded && <span role="alert">Live catalogue unavailable. <button onClick={() => setCatalogueAttempt(n => n+1)}>Reconnect</button> or select Recorded.</span>}
      {message && <span role="status">{message}</span>}
    </div>
    <div className="stage-panes">
      <section className="stage-agent" aria-label="Agent">
        <header><p>BillPal</p><h2>Agent</h2></header>
        {currentCard ? <><p className="stage-prompt">{currentCard.prompt}</p>
          {currentCard.attachment && <details><summary>Attachment</summary><p>{currentCard.attachment}</p></details>}
          {fixture ? <Fixture card={fixture} /> : <AgentResult steps={steps} card={currentCard} stage={mode} />}</>
          : <p>Choose a scenario and start the run.</p>}
        {live && history.status && <p role="status">{history.status}</p>}
      </section>
      <section className="gate-demo stage-gate" aria-label="Gate">
        <header className="stage-gate-header"><h2>Payment gate</h2>
          {live && <span>{connection === "live" ? "Stream connected" : connection}</span>}
          {live && ids.length > 1 && <label>Request<select value={requestChoice || ids.at(-1)} onChange={e => setRequestChoice(e.target.value)}>
            {ids.map(id=><option key={id}>{id}</option>)}</select></label>}
        </header>
        {fixture ? <div className="gate-panel"><h2>Fixture outcome</h2>
          <p>Read-only fixture · {String(fixture.fixture?.reason_code ?? fixture.fixture?.failure_stage ?? "")}</p>
          {fixture.id === "C1" && <p>Identity failed. Agent registry, mandate, policy, risk and bank execution were not reached.</p>}
        </div> : suspendedP5 ? <section className="gate-panel">
          <h2>BillPal was suspended before approval</h2>
          <p>The agent registry blocked this payment before Demo Bank could create an approval.</p>
          <p>A soft reset reinstates BillPal, renews the demo mandate and signs the bank out. Your ledger is retained.</p>
          {selected === "P5" && <button type="button" disabled={busy || waiting} onClick={() => void reset(true)}>Soft reset and rerun P5</button>}
        </section> : outcome.reason === "DEMO_MANDATE_SETUP_FAILED" ? <section className="gate-panel">
          <h2>Payment could not start</h2>
          <p>BillPal could not load or create its demo mandate. Check the gateway connection and demo customer sign-in, then run the scenario again. No payment was submitted.</p>
        </section> : !request && outcome.verdict === "ERROR" ? <section className="gate-panel">
          <h2>Payment outcome unavailable</h2>
          <p>The demo could not confirm this run. Check the gateway connection and bank activity before starting another payment.</p>
        </section> : !request && steps.at(-1)?.name === "completed" && (recorded || !ids.length || steps.at(-1)?.reason?.startsWith("E_AUTH_") || steps.at(-1)?.reason === "E_MANDATE_REVOKED") ? <section className="gate-panel"><h2>No payment trace</h2><p>{steps.at(-1)?.reason ?? "The agent completed without a payment trace."}</p></section> : <Playback key={request?.requestId || "empty"} request={request} recorded={recorded}
          controls={{speed, skip, position: recorded ? frame?.position ?? 0 : undefined}} />}
        {live && feed.historyGap && <p role="status">The gateway replay buffer has a gap.</p>}
      </section>
      <div className="stage-bank" aria-label="Bank">{live ? <DemoBankPage key={epoch} /> : <RecordedBank frame={frame} />}</div>
    </div>
    {showCompare && (
      <dialog className="stage-compare-dialog" open onClose={() => setShowCompare(false)}>
        <div className="stage-compare-header">
          <h2>Side-by-side run comparison</h2>
          <button type="button" onClick={() => setShowCompare(false)}>Close</button>
        </div>
        <p>Compare two recorded scenarios side by side to contrast normal execution with attack prevention.</p>
        <div className="stage-compare-grid">
          <div className="stage-compare-col">
            <label>Run A
              <select value={compareKeyA} onChange={e => setCompareKeyA(e.target.value)}>
                {Object.entries(archive).map(([k, v]) => (
                  <option key={k} value={k}>{title(v.card)}</option>
                ))}
              </select>
            </label>
            <CompareRunSummary capture={archive[compareKeyA]} />
          </div>
          <div className="stage-compare-col">
            <label>Run B
              <select value={compareKeyB} onChange={e => setCompareKeyB(e.target.value)}>
                {Object.entries(archive).map(([k, v]) => (
                  <option key={k} value={k}>{title(v.card)}</option>
                ))}
              </select>
            </label>
            <CompareRunSummary capture={archive[compareKeyB]} />
          </div>
        </div>
      </dialog>
    )}
  </div>;
}

