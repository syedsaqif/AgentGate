/// <reference types="vite/client" />

interface ImportMetaEnv {
  readonly VITE_GATEWAY_ORIGIN?: string;
  readonly VITE_DISPUTE_VERIFIER_URL?: string;
  readonly VITE_DEMO_GATE_STREAM_URL?: string;
  readonly VITE_DEMO_BANK_ORIGIN?: string;
  readonly VITE_DEMO_AGENT_ORIGIN?: string;
}
