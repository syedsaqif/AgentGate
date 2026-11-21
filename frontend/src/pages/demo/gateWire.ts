import { isReceipt } from "../../api/approvalWire";
import { isRecord } from "../../api/shellWire";
import type { SseFrame } from "../../api/sse";

export const stages = ["edge", "identity", "registry", "canonical", "idempotency", "mandate",
  "policy", "risk", "breaker", "decision", "execution", "approval"] as const;
type Stage = typeof stages[number];
const statuses = ["pass", "fail", "skip", "info", "warn", "step_up"] as const;
type Status = typeof statuses[number];
const noticeTypes = ["policy.decision", "action.denied", "escalation.created", "escalation.resolved",
  "bank.executed", "bank.failed", "bank.unknown", "agent.suspended"] as const;
type NoticeType = typeof noticeTypes[number];
type Data = Record<string, unknown>;

type Envelope = { id: string; requestId: string; agentId?: string; occurredAt?: number };
export type GateStep = Envelope & {
  type: "gate.step"; stage: Stage; check: string; status: Status; seq: number; at?: number; detail: Data;
};
export type GateNotice = Envelope & { type: NoticeType; data: Data };
export type GateEvent = GateStep | GateNotice;
export type Decision = "ALLOW" | "ESCALATE" | "DENY";
export type Band = "LOW" | "MEDIUM" | "HIGH";
export type RiskProfile = "balanced" | "conservative" | "permissive";
export type AuditEntry = { seq: number; hash: string; previous?: string; label: string };
export type RequestSummary = {
  agentName?: string; agentId?: string; fingerprint?: string; action?: string; payee?: string;
  amount?: number; category?: string; idempotencyKey?: string; profile?: RiskProfile;
};
export type RequestEvidence = { summary: RequestSummary; audit: AuditEntry[] };

export const textValue = (value: unknown) => typeof value === "string" && value.trim() ? value : undefined;
export const reasonCode = (value: unknown) => typeof value === "string" && /^E_[A-Z\d_]+$/.test(value) ? value : undefined;
export const numberValue = (value: unknown, max = Number.MAX_VALUE) =>
  typeof value === "number" && value >= 0 && value <= max ? value : undefined;
export const integer = (value: unknown) => Number.isSafeInteger(value) ? numberValue(value) : undefined;
export const decisionValue = (value: unknown): Decision | undefined =>
  value === "ALLOW" || value === "ESCALATE" || value === "DENY" ? value : undefined;
export const bandValue = (value: unknown): Band | undefined =>
  value === "LOW" || value === "MEDIUM" || value === "HIGH" ? value : undefined;

export function instant(value: unknown) {
  if (typeof value !== "string" || !/^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/.test(value)) return;
  const ms = Date.parse(value);
  return Number.isFinite(ms) ? ms : undefined;
}

export function decodeGateEnvelope(value: unknown, frame?: SseFrame): GateEvent | undefined {
  if (!isRecord(value) || !isRecord(value.data)) return;
  const { request_id, event_id, event_type, agent_id, occurred_at, data } = value;
  const id = textValue(event_id) ?? frame?.id;
  const requestId = textValue(request_id);
  if (!id || !requestId || (frame && (frame.id !== id || frame.event !== event_type))) return;
  const envelope = { id, requestId, agentId: textValue(agent_id), occurredAt: instant(occurred_at) };
  if (event_type === "gate.step") {
    const stage = stages.find((entry) => entry === data.stage);
    const status = statuses.find((entry) => entry === data.status);
    const check = textValue(data.check);
    const seq = integer(data.seq);
    if (!stage || !status || !check || !seq || !isRecord(data.detail)) return;
    return { ...envelope, type: event_type, stage, check, status, seq, at: instant(data.at), detail: data.detail };
  }
  const type = noticeTypes.find((entry) => entry === event_type);
  if (type) return { ...envelope, type, data };
}

export function decodeGateFrame(frame: SseFrame) {
  try {
    return decodeGateEnvelope(JSON.parse(frame.data), frame);
  } catch {
    return undefined;
  }
}

export function readEvidence(value: unknown): RequestEvidence {
  const request = isRecord(value) && isRecord(value.request) ? value.request : {};
  const { risk_profile } = request;
  const rows = isRecord(value) && Array.isArray(value.audit) ? value.audit : [];
  return {
    summary: {
      agentName: textValue(request.agent_name), agentId: textValue(request.agent_id),
      fingerprint: textValue(request.fingerprint), action: textValue(request.action_type),
      payee: textValue(request.payee_id), amount: integer(request.amount_paise), category: textValue(request.category),
      idempotencyKey: textValue(request.idempotency_key),
      profile: risk_profile === "balanced" || risk_profile === "conservative" || risk_profile === "permissive"
        ? risk_profile : undefined,
    },
    audit: rows.flatMap((row: unknown) => {
      if (!isRecord(row)) return [];
      const label = textValue(row.event_type);
      const previous = typeof row.prev_hash === "string" && /^[0-9a-f]{64}$/.test(row.prev_hash) ? row.prev_hash : undefined;
      return isReceipt(row) && label ? [{ seq: row.seq, hash: row.entry_hash, label, previous }] : [];
    }),
  };
}
