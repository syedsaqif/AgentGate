import { addBankNotice, decodeBankNotice, parseOverview, type BankNotice, type Overview } from "../bank/bankWire";
import { decodeGateEnvelope } from "../gateWire";
import { buildPlayback, emptyFeed, gateFeedReducer, type RequestTimeline } from "../gateTimeline";
import { decodeStep, type Scenario, type Step } from "../agent/agentApi";

type Snapshot = { at: string; overview: unknown };
type Envelope = { event_type: string; event_id: string };
export type Capture = {
  card: Scenario & { aliases?: string[] }; run_id?: string; prerequisite?: string;
  opening?: Snapshot; steps?: {id: string; data: unknown}[];
  phases?: (Snapshot & {gate: Envelope[]; notices: Envelope[]})[];
};
const files = import.meta.glob<Capture>("./recordings/*.json", {eager: true, import: "default"});
export const archive = Object.fromEntries(Object.entries(files).map(([path, capture]) =>
  [path.split("/").at(-1)!.replace(".json", ""), capture]));

export type Frame = {
  steps: Step[]; bank?: Overview; at?: number; notices: BankNotice[];
  request?: RequestTimeline; position: number; delay: number;
};

export function appendStep(steps: Step[], step: Step) {
  return steps.some(x => x.seq === step.seq) ? steps : [...steps, step].sort((a,b) => a.seq - b.seq);
}

// Schedule major observed transitions. Measured timestamps and real deadlines stay in the evidence.
export function replayFrames(record: Capture): Frame[] {
  const frames: Frame[] = [];
  let frame: Frame = {steps: [], bank: parseOverview(record.opening?.overview),
    at: record.opening && Date.parse(record.opening.at), notices: [], position: 0, delay: 220};
  function next(change: Partial<Frame>) {
    frame = {...frame, ...change};
    frames.push(frame);
  }
  next({});
  const steps = (record.steps ?? []).flatMap(raw => {
    const step = decodeStep({id: raw.id, event: "agent_step", data: JSON.stringify(raw.data)});
    return step ? [step] : [];
  });
  let stepIndex = 0;
  function advanceAgent(until: (step: Step) => boolean) {
    while (stepIndex < steps.length && until(steps[stepIndex])) {
      next({steps: appendStep(frame.steps, steps[stepIndex++]), delay: 180});
    }
  }
  let feed = emptyFeed;
  for (const [phaseIndex, phase] of (record.phases ?? []).entries()) {
    const before = feed;
    const touched = new Set<string>();
    for (const raw of phase.gate) {
      const event = decodeGateEnvelope(raw);
      if (!event) continue;
      feed = gateFeedReducer(feed, {type: "frame", event});
      touched.add(event.requestId);
    }
    for (const id of touched) {
      if (!phaseIndex) advanceAgent(s => s.name !== "tool_result" && s.name !== "completed");
      const request = feed.requests.get(id)!;
      const beats = buildPlayback(request);
      const previous = before.requests.get(id);
      for (let i = previous ? buildPlayback(previous).length : 0; i < beats.length; i++) {
        next({request, position: i + 1, delay: beats[i].moment ? 600 : 290});
      }
      if (!phaseIndex && steps[stepIndex]?.name === "tool_result") {
        next({steps: appendStep(frame.steps, steps[stepIndex++]), delay: 210});
      }
    }
    if (!phaseIndex) advanceAgent(() => true);
    const bank = parseOverview(phase.overview);
    if (!bank) throw new Error("Invalid recorded bank snapshot.");
    let notices = frame.notices;
    for (const raw of phase.notices) {
      const item = decodeBankNotice({id: raw.event_id, event: raw.event_type, data: JSON.stringify(raw)}, bank);
      if (item) notices = addBankNotice(notices, item);
    }
    next({bank, at: Date.parse(phase.at), notices, delay: phaseIndex === 0 ? 1450 : 350});
  }
  return frames;
}

