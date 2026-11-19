import type { components, operations, paths } from "./generated/openapi";
import { isGatewayTimestamp } from "./mandateWire";
import { isRecord } from "./shellWire";

export const opsStreamPath = "/v1/ops/stream" satisfies keyof paths;
export const suspendAgentPath = "/v1/ops/agents/{id}/suspend" satisfies keyof paths;
export const reinstateAgentPath = "/v1/ops/agents/{id}/reinstate" satisfies keyof paths;
export type AgentCursor = components["parameters"]["Cursor"];
export type SuspendAgentParams = operations["suspendAgent"]["parameters"]["path"];
export type ReinstateAgentParams = operations["reinstateAgent"]["parameters"]["path"];
export type AgentMutation = "suspend" | "reinstate";

// A owns the §6.8 stream envelope. A/C need to confirm the provisional
// decision fields in data, based on Decision, RiskResult and RiskSignal.
export type OpsDecision = {
  id: string;
  decision: "ALLOW" | "DENY" | "ESCALATE";
  requestId: string;
  agentId: string;
  occurredAt: string;
  score?: number;
  reasons: string[];
};

export type OpsEvent = { kind: "resync"; id?: string }
  | { kind: "decision"; id: string; decision: OpsDecision }
  | { kind: "notification"; id: string; refreshAgents: boolean };

export function decodeOpsEvent(frame: { id?: string; event: string; data: string }): OpsEvent | undefined {
  // The plan leaves the resync signal's discriminator unspecified.
  if (frame.event === "resync") return { kind: "resync", id: frame.id };
  if (!frame.id) return;

  let envelope: unknown;
  try {
    envelope = JSON.parse(frame.data);
  } catch {
    return;
  }
  if (!isRecord(envelope) || envelope.event_id !== frame.id || envelope.event_type !== frame.event
    || !isRecord(envelope.data)) return;

  const { data, request_id, agent_id, occurred_at } = envelope;
  const notification = {
    kind: "notification", id: frame.id,
    refreshAgents: frame.event.startsWith("agent.") || frame.event === "risk.breaker_tripped",
  } as const;
  if ((data.decision !== "ALLOW" && data.decision !== "DENY" && data.decision !== "ESCALATE")
    || typeof request_id !== "string" || !request_id || typeof agent_id !== "string" || !agent_id
    || !isGatewayTimestamp(occurred_at)) return notification;

  const risk = isRecord(data.risk) ? data.risk : undefined;
  const score = risk?.score;
  const signals = Array.isArray(risk?.signals)
    ? risk.signals.flatMap((signal: unknown) => isRecord(signal) && typeof signal.message === "string" ? [signal.message] : [])
    : [];
  const codes = Array.isArray(data.reason_codes)
    ? data.reason_codes.filter((code: unknown): code is string => typeof code === "string")
    : [];
  return {
    kind: "decision", id: frame.id,
    decision: {
      id: frame.id, decision: data.decision, requestId: request_id, agentId: agent_id, occurredAt: occurred_at,
      score: typeof score === "number" && Number.isFinite(score) && score >= 0 && score <= 100 ? score : undefined,
      reasons: signals.length ? signals : codes,
    },
  };
}
