"""Member A OpenAPI export utility and frozen contract comparison."""

from __future__ import annotations

import argparse
import json
from pathlib import Path
from typing import Any

from gateway.main import create_app

ROOT = Path(__file__).resolve().parents[1]
FROZEN_PATH = ROOT / "contracts" / "openapi.json"


def load_frozen_openapi() -> dict[str, Any]:
    with FROZEN_PATH.open("r", encoding="utf-8") as f:
        return json.load(f)


def export_real_openapi(profile: str = "test") -> dict[str, Any]:
    app = create_app(profile=profile)
    return app.openapi()


def compare_openapi(
    frozen: dict[str, Any], real: dict[str, Any]
) -> dict[str, Any]:
    frozen_paths: dict[str, dict[str, Any]] = frozen.get("paths", {})
    real_paths: dict[str, dict[str, Any]] = real.get("paths", {})

    frozen_keys = set(frozen_paths.keys())
    real_keys = set(real_paths.keys())

    matched_paths = sorted(frozen_keys & real_keys)
    missing_paths = sorted(frozen_keys - real_keys)
    extra_paths = sorted(real_keys - frozen_keys)

    method_mismatches: list[dict[str, Any]] = []
    status_mismatches: list[dict[str, Any]] = []

    for path in matched_paths:
        f_methods = set(frozen_paths[path].keys()) - {"parameters"}
        r_methods = set(real_paths[path].keys()) - {"parameters"}

        if f_methods != r_methods:
            method_mismatches.append(
                {
                    "path": path,
                    "expected_methods": sorted(f_methods),
                    "actual_methods": sorted(r_methods),
                }
            )

        common_methods = f_methods & r_methods
        for m in common_methods:
            f_responses = set(
                frozen_paths[path][m].get("responses", {}).keys()
            )
            r_responses = set(real_paths[path][m].get("responses", {}).keys())
            if f_responses != r_responses:
                status_mismatches.append(
                    {
                        "path": path,
                        "method": m.upper(),
                        "expected_statuses": sorted(f_responses),
                        "actual_statuses": sorted(r_responses),
                    }
                )

    return {
        "total_frozen_paths": len(frozen_keys),
        "total_real_paths": len(real_keys),
        "matched_paths": matched_paths,
        "missing_paths": missing_paths,
        "extra_paths": extra_paths,
        "method_mismatches": method_mismatches,
        "status_mismatches": status_mismatches,
    }


def print_report(diff: dict[str, Any]) -> None:
    print("=" * 60)
    print("AgentGate OpenAPI Contract Comparison Report")
    print("=" * 60)
    print(
        f"Frozen paths: {diff['total_frozen_paths']} | Real app paths: {diff['total_real_paths']}"
    )
    print(f"Matched paths: {len(diff['matched_paths'])}")
    print(f"Missing from real app: {len(diff['missing_paths'])}")
    print(f"Extra in real app: {len(diff['extra_paths'])}")
    print("-" * 60)

    if diff["matched_paths"]:
        print("\nMatched Routes:")
        for p in diff["matched_paths"]:
            print(f"  [OK] {p}")

    if diff["missing_paths"]:
        print("\nUnmounted / Missing Routes (Pending Teammate Implementations):")
        for p in diff["missing_paths"]:
            is_b = any(k in p for k in ("oauth", "agents", "login", "audit"))
            owner = "Member B" if is_b else "Unassigned"
            print(f"  [PENDING ({owner})] {p}")

    if diff["extra_paths"]:
        print("\nExtra Routes in Real App:")
        for p in diff["extra_paths"]:
            print(f"  [EXTRA] {p}")

    if diff["method_mismatches"]:
        print("\nMethod Mismatches on Matched Paths:")
        for m in diff["method_mismatches"]:
            print(
                f"  {m['path']}: Expected {m['expected_methods']}, Got {m['actual_methods']}"
            )

    print("=" * 60)


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Export and compare real FastAPI OpenAPI schema against frozen contract"
    )
    parser.add_argument(
        "--profile",
        default="test",
        choices=("test", "scripted", "real"),
        help="Gateway profile to inspect",
    )
    parser.add_argument(
        "--output",
        type=Path,
        default=None,
        help="Optional path to write exported OpenAPI JSON (will not overwrite contracts)",
    )
    args = parser.parse_args()

    real_spec = export_real_openapi(profile=args.profile)
    frozen_spec = load_frozen_openapi()
    diff = compare_openapi(frozen_spec, real_spec)
    print_report(diff)

    if args.output:
        if args.output.resolve() == FROZEN_PATH.resolve():
            raise SystemExit("Refusing to overwrite frozen contracts/openapi.json!")
        with args.output.open("w", encoding="utf-8") as f:
            json.dump(real_spec, f, indent=2)
        print(f"\nExported real OpenAPI JSON to {args.output}")


if __name__ == "__main__":
    main()
