import type { paths } from "./generated/openapi";

export { CONTRACT_VERSION } from "./generated/openapi";

export type PathForMethod<Method extends "get" | "post"> = {
  [Path in keyof paths]: Method extends keyof paths[Path] ? Path : never;
}[keyof paths];

export interface ApiTransport {
  request(path: string, init: RequestInit): Promise<Response>;
}
