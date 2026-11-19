"""Member A foundation test suite: app factory, DI, errors, and SQLite transactions."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
from fastapi import APIRouter
from httpx import ASGITransport, AsyncClient
from pydantic import BaseModel, StrictInt
from sqlalchemy import Column, Integer, String, text

from contracts.common import ErrorEnvelope
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError
from gateway.main import create_app
from gateway.platform.db import (
    Base,
    SqliteUnitOfWork,
    create_db_engine,
    create_session_factory,
)


class DummyPayload(BaseModel):
    amount_paise: StrictInt
    description: str


# Test ORM model for UoW transaction tests
class SampleRecord(Base):
    __tablename__ = "test_records"
    id = Column(Integer, primary_key=True, autoincrement=True)
    name = Column(String(50), nullable=False)


@pytest.fixture
def app_with_test_routes() -> Any:
    app = create_app(profile="test")
    test_router = APIRouter(prefix="/test")

    @test_router.post("/validate")
    async def endpoint_validate(payload: DummyPayload) -> dict[str, Any]:
        return {"amount_paise": payload.amount_paise}

    @test_router.get("/error/{code}")
    async def endpoint_error(code: str) -> None:
        if code == "unhandled":
            raise RuntimeError("simulated crash")
        raise AgentGateError(code=code, message=f"Test error for {code}")

    app.include_router(test_router)
    return app


@pytest.mark.asyncio
async def test_app_creation_test_profile(app_with_test_routes: Any) -> None:
    """Verify application boots cleanly under test profile with health endpoints."""
    transport = ASGITransport(app=app_with_test_routes)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # GET /health/live
        res_live = await client.get("/health/live")
        assert res_live.status_code == 200
        assert res_live.json() == {"status": "ok"}
        assert "X-Request-ID" in res_live.headers
        assert res_live.headers["X-Request-ID"].startswith("req_")

        # GET /health/ready
        res_ready = await client.get("/health/ready")
        assert res_ready.status_code == 200
        data = res_ready.json()
        assert data["status"] == "ready"
        assert data["contract_version"] == "1.0.0"
        assert data["profile"] == "test"
        assert data["providers"]["bank"] == "fake"
        assert data["providers"]["identity"] == "fake"


def test_app_creation_real_profile_rejects_fakes() -> None:
    """Startup must refuse to boot under profile 'real' when fake providers are present."""
    with pytest.raises(RuntimeError, match="Refusing startup in profile 'real'"):
        create_app(profile="real")


@pytest.mark.asyncio
async def test_x_request_id_untrusted_header_override(app_with_test_routes: Any) -> None:
    """Server-generated X-Request-ID must never trust incoming client headers."""
    transport = ASGITransport(app=app_with_test_routes)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        spoofed_id = "client-spoofed-req-123"
        res = await client.get("/health/live", headers={"X-Request-ID": spoofed_id})
        assert res.status_code == 200
        assert res.headers["X-Request-ID"] != spoofed_id
        assert res.headers["X-Request-ID"].startswith("req_")


@pytest.mark.asyncio
async def test_error_normalization_validation_error(app_with_test_routes: Any) -> None:
    """FastAPI validation errors must be normalized to HTTP 400 with code E_REQ_INVALID."""
    transport = ASGITransport(app=app_with_test_routes)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        # Send bad payload (float instead of StrictInt, missing description)
        res = await client.post("/test/validate", json={"amount_paise": 12.5})
        assert res.status_code == 400
        body = res.json()
        envelope = ErrorEnvelope.model_validate(body)
        assert envelope.error.code == ReasonCode.E_REQ_INVALID.value
        assert "amount_paise" in envelope.error.message
        assert envelope.error.request_id == res.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_error_normalization_reason_codes(app_with_test_routes: Any) -> None:
    """Verify error envelope normalization across different HTTP statuses."""
    transport = ASGITransport(app=app_with_test_routes)
    async with AsyncClient(transport=transport, base_url="http://test") as client:
        test_cases = [
            ("E_AUTH_MISSING", 401),
            ("E_POLICY_PAYEE_NOT_ALLOWED", 403),
            ("E_REQ_IDEMPOTENCY_CONFLICT", 409),
            ("E_BANK_INSUFFICIENT_FUNDS", 422),
            ("E_REQ_RATE_LIMITED", 429),
            ("E_BANK_UNAVAILABLE", 502),
            ("E_AUDIT_WRITE_FAILED", 503),
            ("unhandled", 500),
        ]

        for code, expected_status in test_cases:
            res = await client.get(f"/test/error/{code}")
            assert res.status_code == expected_status, f"Expected {expected_status} for {code}"
            assert "X-Request-ID" in res.headers
            body = res.json()
            envelope = ErrorEnvelope.model_validate(body)
            expected_code = "E_SERVICE_UNAVAILABLE" if code == "unhandled" else code
            assert envelope.error.code == expected_code
            assert envelope.error.request_id == res.headers["X-Request-ID"]


@pytest.mark.asyncio
async def test_sqlite_wal_pragmas_and_uow(tmp_path: Path) -> None:
    """Verify SQLite WAL mode, foreign keys, busy timeout, and UnitOfWork transactions."""
    db_file = tmp_path / "test_gateway.db"
    db_url = f"sqlite+aiosqlite:///{db_file}"
    engine = create_db_engine(db_url)
    session_factory = create_session_factory(engine)

    # Verify PRAGMAs on live connection
    async with engine.connect() as conn:
        res_wal = await conn.execute(text("PRAGMA journal_mode;"))
        assert res_wal.scalar() == "wal"

        res_fk = await conn.execute(text("PRAGMA foreign_keys;"))
        assert res_fk.scalar() == 1

        res_timeout = await conn.execute(text("PRAGMA busy_timeout;"))
        assert res_timeout.scalar() == 5000

        res_sync = await conn.execute(text("PRAGMA synchronous;"))
        assert res_sync.scalar() == 1  # NORMAL

    # Create tables
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    uow = SqliteUnitOfWork(session_factory)

    # 1. Test clean commit
    async with uow as tx:
        assert tx.id.startswith("tx_")
        record = SampleRecord(name="Alice")
        tx.session.add(record)

    # Verify committed
    async with session_factory() as verify_session:
        stmt = text("SELECT name FROM test_records WHERE name = 'Alice';")
        result = await verify_session.execute(stmt)
        assert result.scalar() == "Alice"

    # 2. Test rollback on exception
    with pytest.raises(ValueError, match="simulated failure"):
        async with uow as tx:
            record_bad = SampleRecord(name="Bob")
            tx.session.add(record_bad)
            raise ValueError("simulated failure")

    # Verify rolled back
    async with session_factory() as verify_session:
        stmt_bob = text("SELECT name FROM test_records WHERE name = 'Bob';")
        result_bob = await verify_session.execute(stmt_bob)
        assert result_bob.scalar() is None

    await engine.dispose()
