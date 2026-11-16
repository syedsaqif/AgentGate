.PHONY: check test test-a test-b test-c frontend-check

check:
	uv run python -m tools.dev check-contracts

test:
	uv run pytest -m "scaffold or contract"

test-a:
	uv run pytest tests/a

test-b:
	uv run pytest tests/b

test-c:
	uv run pytest tests/c

frontend-check:
	cd frontend && npm ci && npm run typecheck && npm test -- --run && npm run build
