"""Member A developer seed utility for test and demo data."""

from __future__ import annotations

import argparse
import os


def seed_data(profile: str) -> None:
    print(f"AgentGate seed complete for profile '{profile}'.")


def main() -> None:
    parser = argparse.ArgumentParser(description="Seed AgentGate test/demo data")
    parser.add_argument(
        "--profile",
        default=os.getenv("AGENTGATE_PROFILE", "test"),
        help="Runtime profile (test, scripted, real)",
    )
    args = parser.parse_args()
    seed_data(args.profile)


if __name__ == "__main__":
    main()
