import {
  approvalPath, decisionPath, isApprovalPage, observeDecision,
  type ApprovalQuery, type CustomerDecision, type DecisionPathParams, type DecisionRequest,
} from "./approvalWire";
import {
  auditEntriesPath, auditEvidencePath, auditFilterKeys, auditVerifyPath, isAuditPage, isEvidenceBundle, isVerifyReport,
  type AuditCursor, type AuditFilters, type EvidencePathParams,
} from "./auditWire";
import { ApiError, errorFromResponse, redactPin } from "./errors";
import type { operations } from "./generated/openapi";
import { isMandatePage, mandatePath, revokePath, type CreateMandateBody, type RevokePathParams } from "./mandateWire";
import {
  decodeOpsEvent, opsStreamPath, reinstateAgentPath, suspendAgentPath,
  type AgentCursor, type AgentMutation, type OpsEvent, type ReinstateAgentParams, type SuspendAgentParams,
} from "./opsWire";
import { readSse, type SseFrame } from "./sse";
import {
  isCustomerBootstrap,
  isHumanSession,
  isRegisteredAgents,
  shellRoutes,
  type CustomerLogin,
  type OpsLogin,
  type Role,
} from "./shellWire";
import type { ApiTransport, PathForMethod } from "./types";

export type ShellApi = ReturnType<typeof createShellApi>;

type ShellRequest =
  | { path: Exclude<PathForMethod<"post">, typeof revokePath | typeof decisionPath
      | typeof suspendAgentPath | typeof reinstateAgentPath>; method: "POST" }
  | { path: typeof revokePath; method: "POST"; params: RevokePathParams }
  | { path: typeof decisionPath; method: "POST"; params: DecisionPathParams }
  | { path: typeof suspendAgentPath; method: "POST"; params: SuspendAgentParams }
  | { path: typeof reinstateAgentPath; method: "POST"; params: ReinstateAgentParams }
  | { path: Exclude<PathForMethod<"get">, typeof auditEvidencePath>; method: "GET" }
  | { path: typeof auditEvidencePath; method: "GET"; params: EvidencePathParams };

export function createShellApi(transport: ApiTransport) {
  function send(target: ShellRequest, init: Omit<RequestInit, "method">, query = "") {
    let path: string = target.path;
    if (target.path === auditEvidencePath) {
      path = target.path.replace("{request_id}", encodeURIComponent(target.params.request_id));
    } else if ("params" in target) {
      path = target.path.replace("{id}", encodeURIComponent(target.params.id));
    }
    return transport.request(path + query, { ...init, method: target.method });
  }

  async function request<T>(
    target: ShellRequest,
    init: Omit<RequestInit, "method">,
    isValid: (value: unknown) => value is T,
    query?: string,
  ): Promise<T> {
    const response = await send(target, init, query);
    const payload: unknown = await response.json().catch(() => null);
    if (!response.ok || !isValid(payload)) throw errorFromResponse(response, payload);
    return payload;
  }

  async function mutate(target: ShellRequest, init: Omit<RequestInit, "method">, expectedStatus: number) {
    const response = await send(target, init);
    if (response.status !== expectedStatus) {
      const payload: unknown = await response.json().catch(() => null);
      throw errorFromResponse(response, payload);
    }
  }

  const authorized = (token: string, signal: AbortSignal) => ({
    headers: { Accept: "application/json", Authorization: `Bearer ${token}` },
    signal,
  });

  async function stream(path: "/v1/customer/stream" | typeof opsStreamPath, token: string,
    lastEventId: string | undefined, signal: AbortSignal, opened: () => void, event: (frame: SseFrame) => void) {
    const auth = authorized(token, signal);
    const headers = new Headers({ ...auth.headers, Accept: "text/event-stream" });
    if (lastEventId) headers.set("Last-Event-ID", lastEventId);
    const response = await send({ path, method: "GET" }, { ...auth, headers });
    if (!response.ok) {
      const payload: unknown = await response.json().catch(() => null);
      throw errorFromResponse(response, payload);
    }
    if (!response.body || response.headers.get("Content-Type")?.split(";")[0].trim().toLowerCase() !== "text/event-stream") {
      void response.body?.cancel().catch(() => undefined);
      throw new ApiError("The gateway did not return an event stream.", "UI_INVALID_RESPONSE",
        response.headers.get("X-Request-ID") ?? undefined);
    }
    if (signal.aborted) {
      void response.body.cancel().catch(() => undefined);
      return;
    }
    opened();
    await readSse(response.body, signal, event);
  }

  return {
    login(role: Role, id: string, secret: string, signal: AbortSignal) {
      const body: CustomerLogin | OpsLogin = role === "customer"
        ? { customer_id: id, pin: secret }
        : { operator_id: id, password: secret };
      return request({ path: shellRoutes[role].login, method: "POST" }, {
        headers: { Accept: "application/json", "Content-Type": "application/json" },
        body: JSON.stringify(body),
        signal,
      }, (value) => isHumanSession(value, role));
    },
    customerBootstrap(token: string, signal: AbortSignal) {
      return request({ path: shellRoutes.customer.read, method: "GET" },
        authorized(token, signal), isCustomerBootstrap);
    },
    registeredAgents(token: string, signal: AbortSignal, cursor: AgentCursor = null) {
      const query = cursor === null ? "" : `?${new URLSearchParams({ cursor })}`;
      return request({ path: shellRoutes.ops.read, method: "GET" },
        authorized(token, signal), isRegisteredAgents, query);
    },
    auditEntries(token: string, filters: AuditFilters, cursor: AuditCursor, signal: AbortSignal) {
      const query = new URLSearchParams();
      if (cursor != null) query.set("cursor", cursor);
      for (const key of auditFilterKeys) {
        const value = filters[key];
        if (value) query.set(key, value);
      }
      return request({ path: auditEntriesPath, method: "GET" }, authorized(token, signal), isAuditPage,
        query.size ? `?${query}` : "");
    },
    verifyAudit(token: string, signal: AbortSignal) {
      return request({ path: auditVerifyPath, method: "GET" }, authorized(token, signal), isVerifyReport);
    },
    async disputeProof(token: string, requestId: string, signal: AbortSignal) {
      if (!/^req_[a-zA-Z0-9_-]{1,64}$/.test(requestId)) {
        throw new ApiError("This request has no exportable payment disclosure.", "UI_INVALID_RESPONSE");
      }
      const response = await transport.request(`/v1/audit/proof/${encodeURIComponent(requestId)}`, {
        ...authorized(token, signal), method: "GET", cache: "no-store",
      });
      if (!response.ok) throw errorFromResponse(response, await response.json().catch(() => null));
      return response.text();
    },
    async auditEvidence(token: string, requestId: string, signal: AbortSignal) {
      if (!requestId || requestId === "." || requestId === "..") {
        throw new ApiError("The evidence request identifier is invalid.", "UI_INVALID_RESPONSE");
      }
      const response = await send({ path: auditEvidencePath, method: "GET", params: { request_id: requestId } },
        authorized(token, signal));
      const json = await response.text();
      let payload: unknown = null;
      try {
        payload = JSON.parse(json);
      } catch {
        // Report invalid JSON using the usual gateway response handling.
      }
      if (!response.ok || !isEvidenceBundle(payload) || payload.request_id !== requestId) {
        throw errorFromResponse(response, payload);
      }
      return { bundle: payload, json };
    },
    updateAgent(token: string, id: string, action: AgentMutation, signal: AbortSignal) {
      if (!id || id === "." || id === "..") throw new ApiError("The agent identifier is invalid.", "UI_INVALID_RESPONSE");
      const path = action === "suspend" ? suspendAgentPath : reinstateAgentPath;
      const status = 200 satisfies keyof operations["suspendAgent" | "reinstateAgent"]["responses"];
      return mutate({ path, method: "POST", params: { id } }, authorized(token, signal), status);
    },
    async streamOps(token: string, lastEventId: string | undefined, signal: AbortSignal, handlers: {
      opened: () => void; event: (event: OpsEvent) => void;
    }) {
      await stream(opsStreamPath, token, lastEventId, signal, handlers.opened, (frame) => {
        const event = decodeOpsEvent(frame);
        if (event) handlers.event(event);
      });
    },
    streamCustomer(token: string, lastEventId: string | undefined, signal: AbortSignal,
      opened: () => void, event: (frame: SseFrame) => void) {
      return stream("/v1/customer/stream", token, lastEventId, signal, opened, event);
    },
    customerMandates(token: string, cursor: string | null, signal: AbortSignal) {
      const query = cursor === null ? "" : `?${new URLSearchParams({ cursor })}`;
      return request({ path: mandatePath, method: "GET" },
        authorized(token, signal), isMandatePage, query);
    },
    async createMandate(token: string, body: CreateMandateBody, signal: AbortSignal) {
      const auth = authorized(token, signal);
      try {
        await mutate({ path: mandatePath, method: "POST" }, {
          ...auth,
          headers: { ...auth.headers, "Content-Type": "application/json" },
          body: JSON.stringify(body),
        }, 201 satisfies keyof operations["createMandate"]["responses"]);
      } catch (error) {
        throw redactPin(error, body.pin, "The gateway rejected the mandate request.");
      }
    },
    revokeMandate(token: string, id: string, signal: AbortSignal) {
      if (!id || id === "." || id === "..") throw new ApiError("The mandate identifier is invalid.", "UI_INVALID_RESPONSE");
      return mutate({ path: revokePath, method: "POST", params: { id } },
        authorized(token, signal), 200 satisfies keyof operations["revokeMandate"]["responses"]);
    },
    pendingApprovals(token: string, cursor: ApprovalQuery["cursor"], signal: AbortSignal) {
      const query = new URLSearchParams({ status: "PENDING" } satisfies ApprovalQuery);
      if (cursor != null) query.set("cursor", cursor);
      return request({ path: approvalPath, method: "GET" },
        authorized(token, signal), isApprovalPage, `?${query}`);
    },
    async decideApproval(token: string, id: string, choice: CustomerDecision, signal: AbortSignal, expectedRequestId?: string) {
      if (!id || id === "." || id === "..") throw new ApiError("The approval identifier is invalid.", "UI_INVALID_RESPONSE");
      const body: DecisionRequest = choice.decision === "APPROVE"
        ? { decision: "APPROVE", pin: choice.pin }
        : choice.suspend_agent ? { decision: "REJECT", suspend_agent: true } : { decision: "REJECT" };
      const auth = authorized(token, signal);
      try {
        const response = await send({ path: decisionPath, method: "POST", params: { id } }, {
          ...auth, headers: { ...auth.headers, "Content-Type": "application/json" }, body: JSON.stringify(body),
        });
        const payload: unknown = await response.json().catch(() => null);
        const statuses = [200, 202] satisfies (keyof operations["decideEscalation"]["responses"])[];
        if (!statuses.some((status) => status === response.status)) throw errorFromResponse(response, payload);
        return observeDecision(payload, response.status, id, expectedRequestId);
      } catch (error) {
        throw redactPin(error, body.pin ?? undefined);
      }
    },
  };
}
