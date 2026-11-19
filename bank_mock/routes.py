"""Member A bank mock route handlers."""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Annotated

from fastapi import APIRouter, Depends, HTTPException, Query, Request
from sqlalchemy.ext.asyncio import AsyncSession

from bank_mock import repository, service
from bank_mock.auth import verify_bank_service_credential
from bank_mock.models import (
    AccountBalanceResponse,
    LedgerEntryItem,
    LedgerPageResponse,
    PaymentRequest,
    TransferRequest,
)
from contracts.core_models import BankResult
from contracts.reason_codes import ReasonCode

router = APIRouter(
    prefix="/bank",
    tags=["bank_mock"],
    dependencies=[Depends(verify_bank_service_credential)],
)


async def get_db_session(request: Request) -> AsyncIterator[AsyncSession]:
    session_factory = request.app.state.session_factory
    async with session_factory() as session:
        yield session


@router.get("/accounts/{account_id}/balance", response_model=AccountBalanceResponse)
async def get_balance(
    account_id: str,
    customer_id: Annotated[str, Query(min_length=1)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> AccountBalanceResponse:
    """Retrieve account balance for verified customer."""
    account = await repository.get_account(session, account_id)
    if not account or account.customer_id != customer_id:
        raise HTTPException(
            status_code=404,
            detail={
                "code": ReasonCode.E_REQ_INVALID.value,
                "message": f"Account '{account_id}' not found for customer '{customer_id}'",
            },
        )
    return AccountBalanceResponse(
        account_id=account.account_id,
        customer_id=account.customer_id,
        balance_paise=account.balance_paise,
        currency="INR",
    )


@router.get(
    "/accounts/{account_id}/transactions", response_model=LedgerPageResponse
)
async def get_transactions(
    account_id: str,
    customer_id: Annotated[str, Query(min_length=1)],
    session: Annotated[AsyncSession, Depends(get_db_session)],
    limit: Annotated[int, Query(ge=1, le=100)] = 50,
    cursor: Annotated[str | None, Query()] = None,
) -> LedgerPageResponse:
    """Retrieve ledger transactions for verified customer."""
    account = await repository.get_account(session, account_id)
    if not account or account.customer_id != customer_id:
        raise HTTPException(
            status_code=404,
            detail={
                "code": ReasonCode.E_REQ_INVALID.value,
                "message": f"Account '{account_id}' not found for customer '{customer_id}'",
            },
        )
    rows, next_cursor = await repository.list_ledger_entries(
        session, account_id=account_id, limit=limit, cursor=cursor
    )
    items = tuple(
        LedgerEntryItem(
            entry_id=row.entry_id,
            account_id=row.account_id,
            entry_type=row.entry_type,  # type: ignore[arg-type]
            amount_paise=row.amount_paise,
            balance_after_paise=row.balance_after_paise,
            reference=row.reference,
            created_at=row.created_at,
        )
        for row in rows
    )
    return LedgerPageResponse(items=items, next_cursor=next_cursor)


@router.post("/payments", response_model=BankResult)
async def post_payment(
    request: PaymentRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> BankResult:
    """Execute bill payment debit idempotently."""
    return await service.process_payment(session, request)


@router.post("/transfers", response_model=BankResult)
async def post_transfer(
    request: TransferRequest,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> BankResult:
    """Execute account transfer debit idempotently."""
    return await service.process_transfer(session, request)


@router.get("/operations/{bank_idempotency_key}", response_model=BankResult)
async def get_operation(
    bank_idempotency_key: str,
    session: Annotated[AsyncSession, Depends(get_db_session)],
) -> BankResult:
    """Lookup recorded bank operation result for idempotent recovery."""
    return await service.lookup_operation(session, bank_idempotency_key)
