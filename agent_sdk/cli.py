"""Member B AgentGate Python SDK CLI (`python -m agent_sdk.cli`)."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Any

import httpx

from .client import AgentGateClient
from .keys import (
    generate_agent_keypair,
    load_private_key_pem,
    save_private_key_pem,
    save_public_jwk,
)
from .tokens import AgentSDKError


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent_sdk.cli",
        description="AgentGate Ed25519 key management and DPoP-signed API client CLI.",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    keygen = sub.add_parser("keygen", help="Generate a new Ed25519 agent key pair.")
    keygen.add_argument("--out", required=True, help="Output PKCS8 PEM path (outside Git).")
    keygen.add_argument("--pub-out", help="Optional output path for public JWK JSON.")
    keygen.add_argument("--kid", help="Optional public JWK key identifier.")
    keygen.add_argument(
        "--overwrite", action="store_true", help="Overwrite existing private key file."
    )

    inspect_cmd = sub.add_parser("inspect-key", help="Print public JWK and RFC 7638 jkt.")
    inspect_cmd.add_argument("--key", required=True, help="Path to Ed25519 private PEM file.")
    inspect_cmd.add_argument("--kid", help="Optional kid to include in printed public JWK.")

    def _add_api_args(cmd: argparse.ArgumentParser) -> None:
        cmd.add_argument("--gateway-url", default="http://127.0.0.1:8000")
        cmd.add_argument("--agent-id", required=True)
        cmd.add_argument("--mandate-id", required=True)
        cmd.add_argument("--key", required=True, help="Path to Ed25519 private PEM file.")

    balance = sub.add_parser("balance", help="Fetch scoped account balance with DPoP proof.")
    _add_api_args(balance)
    balance.add_argument("--account-id", required=True)

    pay_bill = sub.add_parser("pay-bill", help="Submit a signed bill payment request.")
    _add_api_args(pay_bill)
    pay_bill.add_argument("--from-account", required=True)
    pay_bill.add_argument("--payee-id", required=True)
    pay_bill.add_argument("--amount-paise", required=True, type=int)
    pay_bill.add_argument("--task", required=True)
    pay_bill.add_argument("--category", required=True)
    pay_bill.add_argument("--reference")
    pay_bill.add_argument("--note")
    pay_bill.add_argument("--idempotency-key")
    pay_bill.add_argument(
        "--poll",
        action="store_true",
        help="Poll escalation status if the payment returns PENDING_APPROVAL.",
    )
    pay_bill.add_argument("--poll-interval", type=float, default=2.0)
    pay_bill.add_argument("--poll-max-attempts", type=int, default=30)

    poll_esc = sub.add_parser("poll-escalation", help="Poll approval escalation status.")
    _add_api_args(poll_esc)
    poll_esc.add_argument("--escalation-id", required=True)
    poll_esc.add_argument("--interval", type=float, default=2.0)
    poll_esc.add_argument("--max-attempts", type=int, default=30)

    poll_act = sub.add_parser("poll-action", help="Poll action execution status.")
    _add_api_args(poll_act)
    poll_act.add_argument("--request-id", required=True)
    poll_act.add_argument("--interval", type=float, default=2.0)
    poll_act.add_argument("--max-attempts", type=int, default=30)

    return parser


async def _run_api_command(
    args: argparse.Namespace,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> dict[str, Any]:
    keypair = load_private_key_pem(Path(args.key))
    async with AgentGateClient(
        gateway_url=args.gateway_url,
        agent_id=args.agent_id,
        mandate_id=args.mandate_id,
        keypair=keypair,
        transport=transport,
    ) as client:
        if args.command == "balance":
            return await client.get_balance(args.account_id)
        if args.command == "pay-bill":
            result = await client.pay_bill(
                from_account=args.from_account,
                payee_id=args.payee_id,
                amount_paise=args.amount_paise,
                task=args.task,
                category=args.category,
                reference=args.reference,
                note=args.note,
                idempotency_key=args.idempotency_key,
            )
            if args.poll and result.get("escalation_id"):
                polled = await client.poll_escalation(
                    str(result["escalation_id"]),
                    interval_seconds=args.poll_interval,
                    max_attempts=args.poll_max_attempts,
                )
                return {
                    "initial": result,
                    "poll": {
                        "status": polled.status,
                        "attempts": polled.attempts,
                        "timed_out": polled.timed_out,
                        "payload": polled.payload,
                    },
                }
            return result
        if args.command == "poll-escalation":
            polled = await client.poll_escalation(
                args.escalation_id,
                interval_seconds=args.interval,
                max_attempts=args.max_attempts,
            )
            return {
                "status": polled.status,
                "attempts": polled.attempts,
                "timed_out": polled.timed_out,
                "payload": polled.payload,
            }
        if args.command == "poll-action":
            polled = await client.poll_action(
                args.request_id,
                interval_seconds=args.interval,
                max_attempts=args.max_attempts,
            )
            return {
                "status": polled.status,
                "attempts": polled.attempts,
                "timed_out": polled.timed_out,
                "payload": polled.payload,
            }
    raise ValueError(f"Unsupported command: {args.command}")


def main(
    argv: Sequence[str] | None = None,
    *,
    transport: httpx.AsyncBaseTransport | None = None,
) -> int:
    """CLI entrypoint; returns process exit code (0 on success)."""

    parser = _build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)

    try:
        if args.command == "keygen":
            keypair = generate_agent_keypair()
            saved = save_private_key_pem(keypair, args.out, overwrite=args.overwrite)
            pub_path: str | None = None
            if args.pub_out:
                pub_path = str(save_public_jwk(keypair, args.pub_out, kid=args.kid))
            output: dict[str, Any] = {
                "private_key_path": str(saved),
                "public_jwk": keypair.public_jwk(kid=args.kid),
                "jkt": keypair.jkt,
            }
            if pub_path is not None:
                output["public_jwk_path"] = pub_path
            print(json.dumps(output, indent=2))
            return 0

        if args.command == "inspect-key":
            keypair = load_private_key_pem(args.key)
            print(
                json.dumps(
                    {"public_jwk": keypair.public_jwk(kid=args.kid), "jkt": keypair.jkt},
                    indent=2,
                )
            )
            return 0

        result = asyncio.run(_run_api_command(args, transport=transport))
        print(json.dumps(result, indent=2))
        return 0
    except (AgentSDKError, ValueError, FileExistsError, FileNotFoundError) as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
