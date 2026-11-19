"""Member A test doubles conforming to shared protocols."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta

from contracts.common import HumanRole, OpaqueId, Tx
from contracts.core_models import (
    AccountCard,
    BankResult,
    BankStatus,
    CanonicalAction,
    PayeeCard,
)
from contracts.core_ports import BankClient
from contracts.identity_models import (
    AgentPublicIdentity,
    AgentStatus,
    HumanContext,
)
from contracts.identity_ports import AgentRegistry, HumanAuth, TokenService
from contracts.reason_codes import ReasonCode
from gateway.errors import AgentGateError
from gateway.mandates.models import Catalog


@dataclass
class FakeBank(BankClient):
    """Configurable test double for bank operations supporting both success and fault injection."""

    outcome: BankStatus = BankStatus.EXECUTED
    fail_insufficient_funds: bool = False
    fail_idempotency_conflict: bool = False
    fail_unavailable: bool = False
    timeout: bool = False

    calls: list[str] = field(default_factory=list)
    recorded_operations: dict[str, BankResult] = field(default_factory=dict)

    __is_fake__: bool = True

    async def pay_bill(
        self, action: CanonicalAction, bank_idempotency_key: str
    ) -> BankResult:
        self.calls.append(action.request_id)

        if self.fail_idempotency_conflict:
            raise AgentGateError(
                code=ReasonCode.E_REQ_IDEMPOTENCY_CONFLICT,
                message="Simulated idempotency conflict",
                status_code=409,
            )

        if self.fail_unavailable:
            raise AgentGateError(
                code=ReasonCode.E_BANK_UNAVAILABLE,
                message="Simulated bank outage",
                status_code=502,
            )

        if self.timeout:
            result = BankResult(
                status=BankStatus.UNKNOWN,
                bank_idempotency_key=bank_idempotency_key,
                reason_code=ReasonCode.E_BANK_UNAVAILABLE.value,
            )
            self.recorded_operations[bank_idempotency_key] = result
            return result

        if self.fail_insufficient_funds:
            result = BankResult(
                status=BankStatus.REJECTED,
                bank_idempotency_key=bank_idempotency_key,
                reason_code=ReasonCode.E_BANK_INSUFFICIENT_FUNDS.value,
            )
            self.recorded_operations[bank_idempotency_key] = result
            return result

        result = BankResult(
            status=self.outcome,
            bank_operation_id=f"fake_op_{len(self.calls)}",
            bank_idempotency_key=bank_idempotency_key,
            balance_paise=12315000,
            executed_at=datetime.now(UTC),
        )
        self.recorded_operations[bank_idempotency_key] = result
        return result

    async def transfer(
        self, action: CanonicalAction, bank_idempotency_key: str
    ) -> BankResult:
        return await self.pay_bill(action, bank_idempotency_key)

    async def lookup(self, bank_idempotency_key: str) -> BankResult:
        if bank_idempotency_key in self.recorded_operations:
            return self.recorded_operations[bank_idempotency_key]
        return BankResult(
            status=self.outcome,
            bank_idempotency_key=bank_idempotency_key,
        )


@dataclass
class FakeCatalog(Catalog):
    """Test catalog provider resolving trusted accounts and payees."""

    accounts: dict[str, AccountCard] = field(default_factory=dict)
    payees: dict[str, PayeeCard] = field(default_factory=dict)

    __is_fake__: bool = True

    def __post_init__(self) -> None:
        if not self.accounts:
            self.accounts["ACC-001"] = AccountCard(
                account_id="ACC-001",
                masked_label="Savings Account (...001)",
                customer_id="cust_001",
            )
            self.accounts["ACC-002"] = AccountCard(
                account_id="ACC-002",
                masked_label="Second Account (...002)",
                customer_id="cust_002",
            )
        if not self.payees:
            self.payees["BILL-ELEC"] = PayeeCard(
                payee_id="BILL-ELEC",
                display_name="Electricity Board",
                category="utilities.electricity",
                verified=True,
            )
            self.payees["BILL-MOBILE"] = PayeeCard(
                payee_id="BILL-MOBILE",
                display_name="Mobile Telecom",
                category="utilities.telecom",
                verified=True,
            )
            self.payees["BILL-INS"] = PayeeCard(
                payee_id="BILL-INS",
                display_name="Insurance Corp",
                category="financial.insurance",
                verified=True,
            )
            self.payees["PAYEE-X"] = PayeeCard(
                payee_id="PAYEE-X",
                display_name="Unverified Payee X",
                category="personal.transfer",
                verified=False,
            )
            self.payees["PAYEE-CRYPTO"] = PayeeCard(
                payee_id="PAYEE-CRYPTO",
                display_name="Crypto Exchange",
                category="investment.crypto",
                verified=False,
            )

    async def get_account(self, account_id: str) -> AccountCard | None:
        return self.accounts.get(account_id)

    async def get_payee(self, payee_id: str) -> PayeeCard | None:
        return self.payees.get(payee_id)


@dataclass
class FakeHumanAuth(HumanAuth):
    """Test double for human session and PIN verification."""

    sessions: dict[str, HumanContext] = field(default_factory=dict)
    pins: dict[str, str] = field(default_factory=dict)

    __is_fake__: bool = True

    def __post_init__(self) -> None:
        if not self.sessions:
            now = datetime.now(UTC)
            self.sessions["cust_session_001"] = HumanContext(
                session_id="sess_001",
                subject_id="cust_001",
                role=HumanRole.CUSTOMER,
                expires_at=now + timedelta(hours=1),
            )
            self.sessions["cust_session_002"] = HumanContext(
                session_id="sess_002",
                subject_id="cust_002",
                role=HumanRole.CUSTOMER,
                expires_at=now + timedelta(hours=1),
            )
            self.sessions["ops_session_001"] = HumanContext(
                session_id="sess_ops_001",
                subject_id="ops_user_001",
                role=HumanRole.OPS,
                expires_at=now + timedelta(hours=1),
            )
        if not self.pins:
            self.pins["cust_001"] = "4821"
            self.pins["cust_002"] = "1234"

    async def authenticate(
        self, session_token: str, required_role: HumanRole
    ) -> HumanContext:
        ctx = self.sessions.get(session_token)
        if not ctx:
            raise AgentGateError(ReasonCode.E_AUTH_MISSING, "Session not found", 401)
        if ctx.role != required_role:
            raise AgentGateError(
                ReasonCode.E_AUTH_KEY_MISMATCH,
                f"Role mismatch: required {required_role}, got {ctx.role}",
                403,
            )
        return ctx

    async def verify_pin(self, customer_id: OpaqueId, pin: str) -> None:
        expected = self.pins.get(customer_id)
        if expected is None or pin != expected:
            raise AgentGateError(ReasonCode.E_ESC_BAD_PIN, "Incorrect PIN", 403)


@dataclass
class FakeAgentRegistry(AgentRegistry):
    """Test double for registered agents."""

    agents: dict[str, AgentPublicIdentity] = field(default_factory=dict)

    __is_fake__: bool = True

    def __post_init__(self) -> None:
        if not self.agents:
            self.agents["agt_billpal"] = AgentPublicIdentity(
                agent_id="agt_billpal",
                name="BillPal Agent",
                vendor="BillPal Inc",
                public_jwk={"kty": "OKP", "crv": "Ed25519", "x": "test_public_key_x"},
                jkt="jkt_billpal_test",
                max_scopes=("bill_pay", "balance_read", "transactions_read"),
                status=AgentStatus.ACTIVE,
            )
            self.agents["agt_suspended"] = AgentPublicIdentity(
                agent_id="agt_suspended",
                name="Suspended Agent",
                vendor="Suspended Inc",
                public_jwk={"kty": "OKP", "crv": "Ed25519", "x": "test_public_key_susp"},
                jkt="jkt_suspended_test",
                max_scopes=("bill_pay", "transfer"),
                status=AgentStatus.SUSPENDED,
            )

    async def get(self, agent_id: OpaqueId) -> AgentPublicIdentity:
        agent = self.agents.get(agent_id)
        if not agent:
            raise AgentGateError(
                ReasonCode.E_REQ_INVALID, f"Agent '{agent_id}' not found", 404
            )
        return agent

    async def set_status(
        self, agent_id: OpaqueId, status: AgentStatus, tx: Tx
    ) -> None:
        if agent_id in self.agents:
            # Recreate with new status
            cur = self.agents[agent_id]
            self.agents[agent_id] = AgentPublicIdentity(
                agent_id=cur.agent_id,
                name=cur.name,
                vendor=cur.vendor,
                public_jwk=cur.public_jwk,
                jkt=cur.jkt,
                max_scopes=cur.max_scopes,
                status=status,
            )

    async def list_public(self) -> tuple[AgentPublicIdentity, ...]:
        return tuple(self.agents.values())


@dataclass
class FakeTokenService(TokenService):
    """Test double for revoking tokens per mandate or agent."""

    revoked_mandates: list[str] = field(default_factory=list)
    revoked_agents: list[str] = field(default_factory=list)

    __is_fake__: bool = True

    async def revoke_for_mandate(self, mandate_id: OpaqueId, tx: Tx) -> None:
        self.revoked_mandates.append(mandate_id)

    async def revoke_for_agent(self, agent_id: OpaqueId, tx: Tx) -> None:
        self.revoked_agents.append(agent_id)

