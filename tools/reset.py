"""Member A developer reset utility to clear disposable test state."""

from __future__ import annotations

import argparse
import asyncio
import os

import httpx

from gateway.platform.dev import DEFAULT_DEV_KEY


async def reset_dev(base_url: str, credential: str) -> None:
    headers = {"X-AgentGate-Dev-Credential": credential}
    async with httpx.AsyncClient(base_url=base_url) as client:
        try:
            res = await client.post("/v1/dev/reset", headers=headers, json={"seed": True})
            if res.status_code == 200:
                print(f"AgentGate dev state reset successfully at {base_url}.")
            else:
                print(f"Failed to reset dev state: HTTP {res.status_code} {res.text}")
        except Exception as exc:
            print(f"Could not connect to {base_url}: {exc}")


def main() -> None:
    parser = argparse.ArgumentParser(description="Reset AgentGate disposable test state")
    parser.add_argument(
        "--base-url",
        default=os.getenv("AGENTGATE_BASE_URL", "http://127.0.0.1:8000"),
        help="Gateway base URL",
    )
    parser.add_argument(
        "--credential",
        default=os.getenv("AGENTGATE_DEV_CREDENTIAL", DEFAULT_DEV_KEY),
        help="Dev control credential",
    )
    args = parser.parse_args()
    asyncio.run(reset_dev(args.base_url, args.credential))


if __name__ == "__main__":
    main()
