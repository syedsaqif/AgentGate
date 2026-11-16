"""Member B canonical vector verifier."""

from __future__ import annotations

import json
from pathlib import Path

from contracts.canonical import canonical_json, sha256_prefixed


def main() -> None:
    path = Path(__file__).resolve().parents[1] / "contracts/fixtures/b/action_hash_vector.json"
    vector = json.loads(path.read_text(encoding="utf-8"))
    actual = sha256_prefixed(canonical_json(vector["projection"]))
    if actual != vector["expected_action_hash"]:
        raise SystemExit(f"hash mismatch: expected {vector['expected_action_hash']}, got {actual}")
    print(actual)


if __name__ == "__main__":
    main()
