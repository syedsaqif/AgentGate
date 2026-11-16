export const CONTRACT_VERSION = "1.0.0" as const;

export type ErrorEnvelope = {
  error: { code: string; message: string; request_id: string };
};

export type Page<T> = { items: T[]; next_cursor: string | null };

export interface ApiTransport {
  request<T>(method: string, path: string, body?: unknown): Promise<T>;
  subscribe(path: string, onEvent: (event: unknown) => void): () => void;
}
