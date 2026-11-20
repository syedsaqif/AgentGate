import { StrictMode } from "react";
import { createRoot } from "react-dom/client";
import { App } from "./App";
import { createShellApi } from "./api/client";
import { ApiError } from "./api/errors";
import { createGatewayTransport } from "./api/gatewayTransport";
import "./styles.css";

const root = createRoot(document.getElementById("root")!);

async function mount() {
  if (import.meta.env.MODE === "mock") {
    const [{ createMockTransport }, { demoIds }] = await Promise.all([
      import("./mocks/transport"),
      import("./mocks/fixtures"),
    ]);
    const api = createShellApi(createMockTransport());
    root.render(<StrictMode><App mode="mock" api={api} demoIds={demoIds} /></StrictMode>);
    return;
  }

  try {
    const api = createShellApi(createGatewayTransport(import.meta.env.VITE_GATEWAY_ORIGIN));
    root.render(<StrictMode><App mode="real" api={api} /></StrictMode>);
  } catch (error) {
    if (!(error instanceof ApiError)) throw error;
    root.render(<StrictMode><App mode="real" error={error} /></StrictMode>);
  }
}

void mount();
