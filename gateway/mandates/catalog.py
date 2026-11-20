"""Member A trusted accounts and payee catalog."""

from __future__ import annotations

from contracts.core_models import AccountCard, PayeeCard
from gateway.mandates.models import Catalog


class TrustedCatalog(Catalog):
    """In-memory production catalog resolving trusted accounts and payees."""

    def __init__(
        self,
        accounts: dict[str, AccountCard] | None = None,
        payees: dict[str, PayeeCard] | None = None,
    ) -> None:
        self.accounts = (
            accounts
            if accounts is not None
            else {
                "ACC-001": AccountCard(
                    account_id="ACC-001",
                    masked_label="Savings Account (...001)",
                    customer_id="cust_001",
                ),
                "ACC-002": AccountCard(
                    account_id="ACC-002",
                    masked_label="Second Account (...002)",
                    customer_id="cust_002",
                ),
            }
        )
        self.payees = (
            payees
            if payees is not None
            else {
                "BILL-ELEC": PayeeCard(
                    payee_id="BILL-ELEC",
                    display_name="Electricity Board",
                    category="utilities.electricity",
                    verified=True,
                ),
                "BILL-MOBILE": PayeeCard(
                    payee_id="BILL-MOBILE",
                    display_name="Mobile Telecom",
                    category="utilities.telecom",
                    verified=True,
                ),
                "BILL-INS": PayeeCard(
                    payee_id="BILL-INS",
                    display_name="Insurance Corp",
                    category="financial.insurance",
                    verified=True,
                ),
                "PAYEE-X": PayeeCard(
                    payee_id="PAYEE-X",
                    display_name="Unverified Payee X",
                    category="personal.transfer",
                    verified=False,
                ),
                "PAYEE-CRYPTO": PayeeCard(
                    payee_id="PAYEE-CRYPTO",
                    display_name="Crypto Exchange",
                    category="investment.crypto",
                    verified=False,
                ),
            }
        )

    def get_account(self, account_id: str) -> AccountCard | None:
        return self.accounts.get(account_id)

    def get_payee(self, payee_id: str) -> PayeeCard | None:
        return self.payees.get(payee_id)

    async def list_accounts_for_customer(
        self, customer_id: str
    ) -> tuple[AccountCard, ...]:
        return tuple(
            acc for acc in self.accounts.values() if acc.customer_id == customer_id
        )

    async def list_payees(self) -> tuple[PayeeCard, ...]:
        return tuple(self.payees.values())
