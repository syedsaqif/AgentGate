"""Member C seeded persona and identity definitions for simulations and demo."""

from __future__ import annotations

from contracts.core_models import AccountCard, PayeeCard
from contracts.identity_models import AgentPublicIdentity, AgentStatus

# Customers
CUSTOMER_ID_PRIMARY = "cust_001"
CUSTOMER_PIN_PRIMARY = "4821"

# Accounts
ACCOUNT_PRIMARY = AccountCard(
    account_id="ACC-001",
    masked_label="**1234",
    customer_id=CUSTOMER_ID_PRIMARY,
)
ACCOUNT_INITIAL_BALANCE_PAISE = 12_500_000  # ₹125,000.00

# Registered Agents
AGENT_BILLPAL = AgentPublicIdentity(
    agent_id="agt_billpal",
    name="BillPal Utility Assistant",
    vendor="FinCorp Solutions",
    public_jwk={"kty": "OKP", "crv": "Ed25519", "x": "billpal_public_key_bytes_base64"},
    jkt="jkt_billpal_001",
    max_scopes=("bill_pay", "balance_read"),
    status=AgentStatus.ACTIVE,
)

AGENT_PAYWISE = AgentPublicIdentity(
    agent_id="agt_paywise",
    name="PayWise Assistant",
    vendor="PayTech Global",
    public_jwk={"kty": "OKP", "crv": "Ed25519", "x": "paywise_public_key_bytes_base64"},
    jkt="jkt_paywise_001",
    max_scopes=("bill_pay", "transfer", "balance_read"),
    status=AgentStatus.ACTIVE,
)

# Unregistered Rogue Agent Key
ROGUE_AGENT_ID = "agt_rogue"
ROGUE_PUBLIC_JWK = {
    "kty": "OKP",
    "crv": "Ed25519",
    "x": "rogue_unregistered_key_bytes_base64",
}

# Payees
PAYEE_ELECTRICITY = PayeeCard(
    payee_id="BILL-ELEC",
    display_name="State Electricity Board",
    category="utilities",
    verified=True,
)

PAYEE_MOBILE = PayeeCard(
    payee_id="BILL-MOBILE",
    display_name="National Telecom Mobile",
    category="telecom",
    verified=True,
)

PAYEE_INSURANCE = PayeeCard(
    payee_id="BILL-INS",
    display_name="Life Insurance Corporation",
    category="insurance",
    verified=True,
)

PAYEE_UNVERIFIED_X = PayeeCard(
    payee_id="PAYEE-X",
    display_name="Personal Payee X",
    category="personal",
    verified=False,
)

PAYEE_CRYPTO = PayeeCard(
    payee_id="PAYEE-CRYPTO",
    display_name="Crypto Asset Exchange",
    category="investment",
    verified=False,
)

ALL_PAYEES = {
    PAYEE_ELECTRICITY.payee_id: PAYEE_ELECTRICITY,
    PAYEE_MOBILE.payee_id: PAYEE_MOBILE,
    PAYEE_INSURANCE.payee_id: PAYEE_INSURANCE,
    PAYEE_UNVERIFIED_X.payee_id: PAYEE_UNVERIFIED_X,
    PAYEE_CRYPTO.payee_id: PAYEE_CRYPTO,
}

ALL_ACCOUNTS = {
    ACCOUNT_PRIMARY.account_id: ACCOUNT_PRIMARY,
}
