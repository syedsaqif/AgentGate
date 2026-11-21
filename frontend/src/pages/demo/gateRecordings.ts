import { emptyFeed, gateFeedReducer } from "./gateTimeline";
import { decodeGateEnvelope, readEvidence } from "./gateWire";
import allow from "./traces/allow.json";
import approval from "./traces/approval.json";
import breaker from "./traces/breaker.json";
import cap from "./traces/cap.json";
import identity from "./traces/identity.json";

function recording(title: string, raw: { events: unknown[] }, source = "Recorded trace") {
  const feed = raw.events.reduce((state: typeof emptyFeed, entry) =>
    gateFeedReducer(state, { type: "frame", event: decodeGateEnvelope(entry) }), emptyFeed);
  const timeline = [...feed.requests.values()][0];
  return { title, timeline, evidence: readEvidence(raw), source };
}

export const recordings = {
  allow: recording("Routine payment", allow),
  approval: recording("Customer approval", approval),
  breaker: recording("Policy denial and suspension", breaker),
  cap: recording("Per-payment cap denial", cap),
  identity: recording("Identity rejection", identity, "Identity test fixture"),
};

export type RecordingName = keyof typeof recordings;
