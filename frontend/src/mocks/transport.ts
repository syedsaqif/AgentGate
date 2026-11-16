import type { ApiTransport } from "../api/types";

export class UnconfiguredMockTransport implements ApiTransport {
  async request<T>(): Promise<T> {
    throw new Error("Member D must select an explicit success, denial, pending, or failure fixture.");
  }

  subscribe(): () => void {
    throw new Error("Member D must configure mock event delivery and resynchronization.");
  }
}
