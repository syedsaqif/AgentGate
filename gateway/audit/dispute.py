"""Versioned offline disclosure and independently witnessed audit prefix."""

from __future__ import annotations

import re
from collections.abc import Iterable
from pathlib import Path
from typing import Annotated, Any, Literal

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from pydantic import Field, StrictInt, StringConstraints
from sqlalchemy import select

from contracts.audit_models import AuditEvent
from contracts.common import AuditRef, ContractModel
from contracts.core_models import CanonicalAction
from gateway.identity.crypto import (
    GENESIS_PREV_HASH,
    audit_entry_hash,
    canonical_json,
    ed25519_sign_b64url,
    ed25519_verify_b64url,
    jwk_to_public_key,
    public_key_thumbprint,
    sha256_hex,
    verify_audit_entry_signature,
)
from gateway.identity.models import SessionFactory, maybe_await, session_scope

from .models import AuditLogRecord

FORMAT = "agentgate-dispute-v1"
DISCLOSURE_EVENT = "dispute.receipt.v1"
MAX_PREFIX = 10_000
type HexHash = Annotated[str, StringConstraints(pattern=r"^[0-9a-f]{64}$")]
type SequenceNumber = Annotated[StrictInt, Field(ge=1, le=MAX_PREFIX)]
type LogId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9][A-Za-z0-9._-]{0,63}$")]
type KeyId = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{43}$")]
type Signature = Annotated[str, StringConstraints(pattern=r"^[A-Za-z0-9_-]{86}$")]
type Timestamp = Annotated[str, StringConstraints(
    pattern=r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(\.\d{1,6})?Z$"
)]


class ProofError(ValueError):
    """A proof cannot be exported or trusted."""


class PublicKey(ContractModel):
    kty: Literal["OKP"]
    crv: Literal["Ed25519"]
    x: KeyId

    def load(self) -> Ed25519PublicKey:
        return jwk_to_public_key(self.model_dump())


class Trust(ContractModel):
    format: Literal["agentgate-dispute-v1"] = FORMAT
    log_id: LogId
    audit_key: PublicKey
    witness_key: PublicKey

    def validate_keys(self) -> None:
        if public_key_thumbprint(self.audit_key.load()) == public_key_thumbprint(
            self.witness_key.load()
        ):
            raise ProofError("The audit key and witness key must be distinct.")


class PrefixSeal(ContractModel):
    format: Literal["agentgate-dispute-v1"] = FORMAT
    purpose: Literal["audit-prefix", "witness-checkpoint"]
    log_id: LogId
    seq: SequenceNumber
    entry_hash: HexHash
    links_hash: HexHash
    published_at: Timestamp
    key_id: KeyId
    sig: Signature

    def verify(self, key: Ed25519PublicKey) -> bool:
        return self.key_id == public_key_thumbprint(key) and ed25519_verify_b64url(
            key, canonical_json(self.model_dump(exclude={"sig"})), self.sig
        )


class DisclosedEvidence(ContractModel):
    action: Literal["bill_pay", "transfer"]
    currency: Literal["INR"]
    amount_paise: Annotated[StrictInt, Field(ge=1, le=2**53 - 1)]
    outcome: Literal["EXECUTED"]
    receipt_seq: SequenceNumber
    receipt_hash: HexHash


def payment_disclosure(
    event: AuditEvent, action: CanonicalAction, receipt: AuditRef
) -> AuditEvent | None:
    """Produce a fixed public projection with no caller text or account identifiers."""
    request_id = str(action.request_id)
    amount = action.params.amount_paise
    if not re.fullmatch(r"req_[a-f0-9]{16}", request_id):
        return None
    if receipt.seq >= MAX_PREFIX or amount is None or not 0 < amount < 2**53:
        return None
    if action.action_type not in ("bill_pay", "transfer"):
        return None
    evidence = DisclosedEvidence(
        action=action.action_type,
        currency="INR",
        amount_paise=amount,
        outcome="EXECUTED",
        receipt_seq=receipt.seq,
        receipt_hash=receipt.entry_hash,
    )
    return AuditEvent(
        event_type=DISCLOSURE_EVENT,
        request_id=request_id,
        safe_payload=evidence.model_dump(),
        occurred_at=event.occurred_at,
    )


def checked_links(
    rows: Iterable[AuditLogRecord], public_key: Ed25519PublicKey
) -> list[dict[str, Any]]:
    """Verify original RFC 8785 hashes and signatures before sealing opaque chain links."""
    links = []
    prev_hash = GENESIS_PREV_HASH
    for seq, row in enumerate(rows, 1):
        if seq > MAX_PREFIX:
            raise ProofError(f"Version 1 supports up to {MAX_PREFIX} audit records per proof.")
        if row.seq != seq or row.prev_hash != prev_hash:
            raise ProofError(f"Audit continuity failed at sequence {seq}.")
        if row.entry_hash != audit_entry_hash(prev_hash, row.to_canonical_entry()):
            raise ProofError(f"Audit hash failed at sequence {seq}.")
        if not verify_audit_entry_signature(public_key, row.entry_hash, row.sig):
            raise ProofError(f"Audit signature failed at sequence {seq}.")
        links.append({
            "seq": seq, "prev_hash": prev_hash, "entry_hash": row.entry_hash, "sig": row.sig,
        })
        prev_hash = row.entry_hash
    if not links:
        raise ProofError("The audit log is empty.")
    return links


def matches_prefix(seal: PrefixSeal, links: list[dict[str, Any]]) -> bool:
    return (
        len(links) >= seal.seq
        and seal.entry_hash == links[seal.seq - 1]["entry_hash"]
        and seal.links_hash == sha256_hex(canonical_json(links[:seal.seq]))
    )


def seal_prefix(
    *, key: Ed25519PrivateKey, purpose: Literal["audit-prefix", "witness-checkpoint"],
    log_id: str, published_at: str, links: list[dict[str, Any]],
) -> PrefixSeal:
    payload = {
        "format": FORMAT,
        "purpose": purpose,
        "log_id": log_id,
        "seq": len(links),
        "entry_hash": links[-1]["entry_hash"],
        "links_hash": sha256_hex(canonical_json(links)),
        "published_at": published_at,
        "key_id": public_key_thumbprint(key.public_key()),
    }
    return PrefixSeal.model_validate({
        **payload, "sig": ed25519_sign_b64url(key, canonical_json(payload)),
    })


def load_checkpoint(path: Path, trust: Trust) -> PrefixSeal:
    seal = PrefixSeal.model_validate_json(path.read_bytes())
    if (
        seal.purpose != "witness-checkpoint"
        or seal.log_id != trust.log_id
        or not seal.verify(trust.witness_key.load())
    ):
        raise ProofError("Witness checkpoint has an invalid signature, purpose or log ID.")
    return seal


def checked_disclosure(row: AuditLogRecord) -> DisclosedEvidence:
    entry = row.to_canonical_entry()
    if (
        row.event_type != DISCLOSURE_EVENT
        or not re.fullmatch(r"req_[a-f0-9]{16}", row.request_id or "")
        or entry["reason_codes"] != []
        or any(entry[field] is not None for field in (
            "correlation_id", "customer_id", "agent_id", "mandate_id", "mandate_hash",
            "decision", "risk",
        ))
    ):
        raise ProofError("The signed disclosure contains unexpected fields.")
    return DisclosedEvidence.model_validate(entry["safe_payload"])


async def build_proof(
    *, session_factory: SessionFactory, key: Ed25519PrivateKey, request_id: str,
    trust: Trust, checkpoint: PrefixSeal | None, published_at: str,
) -> dict[str, Any]:
    trust.validate_keys()
    if public_key_thumbprint(key.public_key()) != public_key_thumbprint(trust.audit_key.load()):
        raise ProofError("The audit signing key differs from the registered audit key.")

    async with session_scope(session_factory, write=False) as session:
        statement = select(AuditLogRecord).order_by(AuditLogRecord.seq)
        if checkpoint is not None:
            statement = statement.where(AuditLogRecord.seq <= checkpoint.seq)
        result = await maybe_await(session.execute(statement.limit(MAX_PREFIX + 1)))
        rows: list[AuditLogRecord] = list(result.scalars().all())

    links = checked_links(rows, trust.audit_key.load())
    if checkpoint is not None and not matches_prefix(checkpoint, links):
        raise ProofError("The witness checkpoint conflicts with the local audit prefix.")
    for disclosure in reversed(rows):
        if disclosure.event_type == DISCLOSURE_EVENT and disclosure.request_id == request_id:
            break
    else:
        raise ProofError(
            "No signed disclosure is covered. Publish a newer checkpoint for a recent payment."
            if checkpoint is not None else "No signed disclosure exists for this payment."
        )
    evidence = checked_disclosure(disclosure)
    if evidence.receipt_seq != disclosure.seq - 1:
        raise ProofError("The disclosure does not immediately follow the Customer receipt.")
    receipt = rows[evidence.receipt_seq - 1]
    if (
        receipt.event_type != "bank.executed"
        or receipt.request_id != request_id
        or receipt.entry_hash != evidence.receipt_hash
    ):
        raise ProofError("The disclosure conflicts with the Customer receipt.")

    audit_seal = seal_prefix(
        key=key, purpose="audit-prefix", log_id=trust.log_id, published_at=published_at,
        links=links,
    )
    return {
        "format": FORMAT,
        "algorithm": "Ed25519+SHA-256+RFC8785",
        "log_id": trust.log_id,
        "audit_seal": audit_seal.model_dump(),
        "witness_checkpoint": checkpoint.model_dump() if checkpoint is not None else None,
        "links": links,
        "record": disclosure.to_canonical_entry(),
        "evidence": evidence.model_dump(),
    }
