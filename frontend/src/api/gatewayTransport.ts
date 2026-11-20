import { ApiError } from "./errors";
import type { ApiTransport } from "./types";

function gatewayOrigin(configuredOrigin: string | undefined): string {
  try {
    const url = new URL(configuredOrigin ?? "");
    if (["http:", "https:"].includes(url.protocol)
      && !url.username && !url.password && url.href === `${url.origin}/`) {
      return url.origin;
    }
  } catch {
    // Report the configuration problem without displaying the supplied value.
  }
  throw new ApiError(
    "Set VITE_GATEWAY_ORIGIN to an HTTP or HTTPS gateway origin and restart the app.",
    "UI_GATEWAY_CONFIG",
  );
}

export function createGatewayTransport(configuredOrigin: string | undefined): ApiTransport {
  const origin = gatewayOrigin(configuredOrigin);

  return {
    async request(path, init) {
      try {
        return await fetch(`${origin}${path}`, {
          ...init,
          cache: "no-store",
          credentials: "omit",
          redirect: "error",
        });
      } catch (error) {
        if (init.signal?.aborted) throw error;
        throw new ApiError(
          "Could not connect to the gateway. Check its address, availability, and browser access.",
          "UI_GATEWAY_UNREACHABLE",
        );
      }
    },
  };
}
