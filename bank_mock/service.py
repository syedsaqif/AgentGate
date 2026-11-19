"""Member A bank mock business service: seed data, atomic debit, and idempotency."""

from __future__ import annotations

import hashlib
import json
import uuid
from datetime import UTC, datetime
from typing import Any

from fastapi import HTTPException
from sqlalchemy.ext.asyncio import AsyncSession

from bank_mock import repository
from bank_mock.models import PaymentRequest, TransferRequest
from bank_mock.schema import (
    AccountTable,
    BankIdempotencyTable,
    LedgerTable,
    PayeeTable,
)
from contracts.canonical import canonical_json, sha256_prefixed
from contracts.core_models import BankResult, BankStatus
from contracts.reason_codes import ReasonCode


def compute_payload_hash(payload: dict[str, Any]) -> str:
    """Compute deterministic sha256 of request payload for idempotency mismatch checks."""
    raw = json.dumps(payload, sort_keys=True, default=str).encode("utf-8")
    return hashlib.sha256(raw).hexdigest()


async def seed_bank_data(session: AsyncSession) -> None:
    """Seed demo accounts and payees per TEAM_TASK_DISTRIBUTION specifications."""
    # Seed ACC-001 (initial 12,500,000 paise = 125,000 INR)
    existing_acc = await repository.get_account(session, "ACC-001")
    if not existing_acc:
        acc = AccountTable(
            account_id="ACC-001",
            customer_id="cust_001",
            balance_paise=12500000,
            currency="INR",
            created_at=datetime.now(UTC),
        )
        session.add(acc)

    # Seed demo payees
    payees_to_seed = [
        ("BILL-ELEC", "Electricity Board", "utilities.electricity", True),
        ("BILL-MOBILE", "Mobile Telecom", "utilities.telecom", True),
        ("BILL-INS", "Insurance Corp", "financial.insurance", True),
        ("PAYEE-X", "Unverified Payee X", "personal.transfer", False),
        ("PAYEE-CRYPTO", "Crypto Exchange", "investment.crypto", False),
    ]
    for p_id, name, cat, verified in payees_to_seed:
        existing_p = await repository.get_payee(session, p_id)
        if not existing_p:
            p = PayeeTable(
                payee_id=p_id,
                display_name=name,
                category=cat,
                verified=verified,
            )
            session.add(p)

    await session.commit()


async def process_payment(
    session: AsyncSession, request: PaymentRequest
) -> BankResult:
    """Execute bill payment atomically with idempotency protection and balance checking."""
    payload_dict = {
        "action_type": "bill_pay",
        "customer_id": request.customer_id,
        "from_account": request.from_account,
        "payee_id": request.payee_id,
        "amount_paise": request.amount_paise,
        "currency": request.currency,
        "reference": request.reference,
    }
    payload_hash = compute_payload_hash(payload_dict)

    # 1. Idempotency Check
    existing_rec = await repository.get_idempotency_record(
        session, request.bank_idempotency_key
    )
    if existing_rec:
        if existing_rec.request_hash != payload_hash:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": ReasonCode.E_REQ_IDEMPOTENCY_CONFLICT.value,
                    "message": "Idempotency key reused with different payment parameters",
                },
            )
        status_enum = (
            BankStatus.EXECUTED
            if existing_rec.status == "EXECUTED"
            else BankStatus.REJECTED
        )
        return BankResult(
            status=status_enum,
            bank_operation_id=existing_rec.bank_operation_id,
            bank_idempotency_key=existing_rec.bank_idempotency_key,
            reason_code=existing_rec.reason_code,
            balance_paise=existing_rec.balance_paise,
            executed_at=existing_rec.executed_at,
        )

    # 2. Account ownership check
    account = await repository.get_account(session, request.from_account)
    if not account or account.customer_id != request.customer_id:
        raise HTTPException(
            status_code=404,
            detail={
                "code": ReasonCode.E_REQ_INVALID.value,
                "message": f"Account '{request.from_account}' not found for customer",
            },
        )

    # 3. Balance verification
    if account.balance_paise < request.amount_paise:
        raise HTTPException(
            status_code=422,
            detail={
                "code": ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value,
                "message": f"Insufficient funds: balance {account.balance_paise} paise is less than payment {request.amount_paise} paise",
            },
        )

    # 4. Atomic debit & ledger update
    now = datetime.now(UTC)
    account.balance_paise -= request.amount_paise
    op_id = f"bank_op_{uuid.uuid4().hex[:12]}"
    entry_id = f"led_{uuid.uuid4().hex[:12]}"

    ledger_entry = LedgerTable(
        entry_id=entry_id,
        account_id=account.account_id,
        entry_type="DEBIT",
        amount_paise=request.amount_paise,
        balance_after_paise=account.balance_paise,
        reference=request.reference,
        created_at=now,
    )
    session.add(ledger_entry)

    idemp_rec = BankIdempotencyTable(
        bank_idempotency_key=request.bank_idempotency_key,
        action_type="bill_pay",
        request_hash=payload_hash,
        status="EXECUTED",
        bank_operation_id=op_id,
        amount_paise=request.amount_paise,
        balance_paise=account.balance_paise,
        reason_code=None,
        executed_at=now,
    )
    session.add(idemp_rec)
    await session.commit()

    return BankResult(
        status=BankStatus.EXECUTED,
        bank_operation_id=op_id,
        bank_idempotency_key=request.bank_idempotency_key,
        balance_paise=account.balance_paise,
        executed_at=now,
    )


async def process_transfer(
    session: AsyncSession, request: TransferRequest
) -> BankResult:
    """Execute account transfer atomically with idempotency protection and balance checking."""
    payload_dict = {
        "action_type": "transfer",
        "customer_id": request.customer_id,
        "from_account": request.from_account,
        "to_account": request.to_account,
        "amount_paise": request.amount_paise,
        "currency": request.currency,
        "reference": request.reference,
    }
    payload_hash = compute_payload_hash(payload_dict)

    existing_rec = await repository.get_idempotency_record(
        session, request.bank_idempotency_key
    )
    if existing_rec:
        if existing_rec.request_hash != payload_hash:
            raise HTTPException(
                status_code=409,
                detail={
                    "code": ReasonCode.E_REQ_IDEMPOTENCY_CONFLICT.value,
                    "message": "Idempotency key reused with different transfer parameters",
                },
            )
        status_enum = (
            BankStatus.EXECUTED
            if existing_rec.status == "EXECUTED"
            else BankStatus.REJECTED
        )
        return BankResult(
            status=status_enum,
            bank_operation_id=existing_rec.bank_operation_id,
            bank_idempotency_key=existing_rec.bank_idempotency_key,
            reason_code=existing_rec.reason_code,
            balance_paise=existing_rec.balance_paise,
            executed_at=existing_rec.executed_at,
        )

    account = await repository.get_account(session, request.from_account)
    if not account or account.customer_id != request.customer_id:
        raise HTTPException(
            status_code=404,
            detail={
                "code": ReasonCode.E_REQ_INVALID.value,
                "message": f"Account '{request.from_account}' not found for customer",
            },
        )

    if account.balance_paise < request.amount_paise:
        raise HTTPException(
            status_code=422,
            detail={
                "code": ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value,
                "message": f"Insufficient funds: balance {account.balance_paise} paise is less than transfer {request.amount_paise} paise",
            },
        )

    now = datetime.now(UTC)
    account.balance_paise -= request.amount_paise
    op_id = f"bank_op_{uuid.uuid4().hex[:12]}"
    entry_id = f"led_{uuid.uuid4().hex[:12]}"

    ledger_entry = LedgerTable(
        entry_id=entry_id,
        account_id=account.account_id,
        entry_type="DEBIT",
        amount_paise=request.amount_paise,
        balance_after_paise=account.balance_paise,
        reference=request.reference,
        created_at=now,
    )
    session.add(ledger_entry)

    idemp_rec = BankIdempotencyTable(
        bank_idempotency_key=request.bank_idempotency_key,
        action_type="transfer",
        request_hash=payload_hash,
        status="EXECUTED",
        bank_operation_id=op_id,
        amount_paise=request.amount_paise,
        balance_paise=account.balance_paise,
        reason_code=None,
        executed_at=now,
    )
    session.add(idemp_rec)
    await session.commit()

    return BankResult(
        status=BankStatus.EXECUTED,
        bank_operation_id=op_id,
        bank_idempotency_key=request.bank_idempotency_key,
        balance_paise=account.balance_paise,
        executed_at=now,
    )


async def lookup_operation(
    session: AsyncSession, bank_idempotency_key: str
) -> BankResult:
    """Retrieve operation result by bank idempotency key."""
    rec = await repository.get_idempotency_record(session, bank_idempotency_key)
    if not rec:
        raise HTTPException(
            status_code=404,
            detail={
                "code": ReasonCode.E_REQ_INVALID.value,
                "message": f"Bank operation with key '{bank_idempotency_key}' not found",
            },
        )

    status_enum = (
        BankStatus.EXECUTED if rec.status == "EXECUTED" else BankStatus.REJECTED
    )
    return BankResult(
        status=status_enum,
        bank_operation_id=rec.bank_operation_id,
        bank_idempotency_key=rec.bank_idempotency_key,
        reason_code=rec.reason_code,
        balance_paise=rec.balance_paise,
        executed_at=rec.executed_at,
    )
