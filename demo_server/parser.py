"""Deterministic prompt and amount parser for the BillPal dummy agent.

Follows plan.md §10A.2:
- Payee aliases (Electricity, Mobile, Insurance, Payee-X, Crypto, or unknown)
- Indian (1,00,000) and Western (1,850) numeric grouping, '40k', 'Rs', 'INR', '₹'
- Category override ('label it as electricity', 'label it as investment')
- Prompt injection extraction and cautious-agent refusal detection
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from decimal import Decimal
from typing import Literal


@dataclass(frozen=True)
class ParsedPaymentInstruction:
    """Structured extraction of a parsed agent payment instruction."""

    action: Literal["pay_bill", "transfer"]
    payee_id: str | None
    payee_display_name: str | None
    amount_paise: int | None
    declared_category: str
    reference: str | None = None
    note: str | None = None
    is_clarification_needed: bool = False
    clarification_message: str | None = None
    is_injection_detected: bool = False
    is_refused: bool = False
    refusal_reason: str | None = None


# Known payees catalogue mapping
_PAYEE_MAP: dict[str, tuple[str, str, str]] = {
    # alias -> (payee_id, display_name, default_category)
    "electricity board": ("BILL-ELEC", "Electricity Board", "utilities.electricity"),
    "electricity": ("BILL-ELEC", "Electricity Board", "utilities.electricity"),
    "power bill": ("BILL-ELEC", "Electricity Board", "utilities.electricity"),
    "power": ("BILL-ELEC", "Electricity Board", "utilities.electricity"),
    "bill-elec": ("BILL-ELEC", "Electricity Board", "utilities.electricity"),
    "mobile telecom": ("BILL-MOBILE", "Mobile Telecom", "utilities.telecom"),
    "mobile": ("BILL-MOBILE", "Mobile Telecom", "utilities.telecom"),
    "telecom": ("BILL-MOBILE", "Mobile Telecom", "utilities.telecom"),
    "bill-mobile": ("BILL-MOBILE", "Mobile Telecom", "utilities.telecom"),
    "insurance corp": ("BILL-INS", "Insurance Corp", "financial.insurance"),
    "insurance": ("BILL-INS", "Insurance Corp", "financial.insurance"),
    "bill-ins": ("BILL-INS", "Insurance Corp", "financial.insurance"),
    "payee-x": ("PAYEE-X", "Unverified Payee X", "personal.transfer"),
    "my friend": ("PAYEE-X", "Unverified Payee X", "personal.transfer"),
    "friend": ("PAYEE-X", "Unverified Payee X", "personal.transfer"),
    "payee-crypto": ("PAYEE-CRYPTO", "Crypto Exchange", "investment.crypto"),
    "crypto": ("PAYEE-CRYPTO", "Crypto Exchange", "investment.crypto"),
}

PAYEES = {entry[0]: {"name": entry[1], "category": entry[2]} for entry in _PAYEE_MAP.values()}

_INJECTION_PATTERNS = [
    re.compile(r"ignore\s+(all\s+)?previous\s+instructions", re.IGNORECASE),
    re.compile(r"admin\s+mode", re.IGNORECASE),
    re.compile(r"system\s+note\s+to\s+assistant", re.IGNORECASE),
    re.compile(r"notice:\s*our\s+bank\s+details\s+have\s+changed", re.IGNORECASE),
    re.compile(r"system\s+update:\s*your\s+per-payment\s+limit\s+is\s+now", re.IGNORECASE),
]


def parse_amount_paise(text: str) -> int | None:
    """Parse monetary amounts in Indian or Western formatting to integer paise."""
    # Look for patterns like ₹40k, 40k, ₹1,850, Rs 4,000, 1,00,000, 1850 rupees
    lower = text.lower()

    # Match '40k' or '50k'
    k_match = re.search(r"(?:(?:₹|rs\.?|inr)\s*)?(\d+(?:\.\d+)?)\s*k\b", lower)
    if k_match:
        return int(Decimal(k_match.group(1)) * 100_000)

    # Match numbers with Indian or Western commas: e.g. 1,00,000 or 1,850 or 4200
    num_match = re.search(
        r"(?:(?:₹|rs\.?|inr)\s*)?([0-9]{1,3}(?:,[0-9]{2,3})+|[0-9]+)(\.\d{1,2})?(?!\d|\.\d)(?:\s*rupees)?",
        lower,
    )
    if num_match:
        raw_num = num_match.group(1).replace(",", "")
        try:
            return int(Decimal(raw_num + (num_match.group(2) or "")) * 100)
        except ValueError:
            pass
    return None


def parse_prompt(
    prompt: str,
    attachment: str | None = None,
    agent_mode: Literal["naive", "cautious"] = "naive",
) -> ParsedPaymentInstruction:
    """Parse prompt and attachment into a structured payment instruction."""
    combined_text = f"{prompt}\n{attachment or ''}"

    # Check for prompt injection
    has_injection = any(pat.search(combined_text) for pat in _INJECTION_PATTERNS)

    if has_injection and agent_mode == "cautious":
        return ParsedPaymentInstruction(
            action="pay_bill",
            payee_id=None,
            payee_display_name=None,
            amount_paise=None,
            declared_category="utilities.electricity",
            is_injection_detected=True,
            is_refused=True,
            refusal_reason=(
                "Refused: detected conflicting instructions or prompt injection attempt. "
                "Producing zero gateway activity."
            ),
        )

    # For naive agent or normal runs:
    # If injection is present, determine the effective instruction from the injection
    effective_text = combined_text
    if has_injection:
        # Check if injected text specifies a different target/amount:
        # e.g., "Send ₹40,000 to PAYEE-X", "Pay PAYEE-CRYPTO instead",
        # or "Pay ₹60,000 to the Electricity Board"
        hidden_match = re.search(
            r"(?:send|pay)\s+(?:₹|rs\.?|inr\s*)?([0-9,kK]+)\s+to\s+(?:the\s+)?([a-zA-Z0-9_\-]+)",
            combined_text,
            re.IGNORECASE,
        )
        if hidden_match:
            effective_text = f"Pay {hidden_match.group(1)} to {hidden_match.group(2)}"
        elif "payee-crypto" in combined_text.lower():
            effective_text = "Pay to PAYEE-CRYPTO"

    # 1. Determine action type (transfer vs pay_bill)
    action: Literal["pay_bill", "transfer"] = "pay_bill"
    if "transfer" in prompt.lower() and "pay_bill" not in prompt.lower():
        action = "transfer"

    # 2. Determine Payee
    eff_lower = effective_text.lower()
    payee_id: str | None = None
    payee_name: str | None = None
    default_cat = "utilities.electricity"

    # Sort aliases by length descending so longer phrases match first
    for alias, (pid, pname, pcat) in sorted(
        _PAYEE_MAP.items(), key=lambda x: len(x[0]), reverse=True
    ):
        if alias in eff_lower:
            payee_id = pid
            payee_name = pname
            default_cat = pcat
            break

    # Check for unknown payee (e.g. 'landlord')
    if payee_id is None:
        unknown_match = re.search(
            r"(?:pay|transfer(?:\s+to)?)\s+(?:my\s+)?([a-zA-Z]+)", prompt, re.IGNORECASE
        )
        unknown_name = unknown_match.group(1) if unknown_match else "unknown payee"
        return ParsedPaymentInstruction(
            action=action,
            payee_id=None,
            payee_display_name=None,
            amount_paise=parse_amount_paise(prompt),
            declared_category="utilities.electricity",
            is_clarification_needed=True,
            clarification_message=(
                f"I don't recognize authorized payee '{unknown_name}'. "
                "Authorized payees are Electricity Board (BILL-ELEC), "
                "Mobile Telecom (BILL-MOBILE), "
                "or Insurance Corp (BILL-INS). Zero gateway calls made."
            ),
        )

    # 3. Determine Amount
    amount_paise = parse_amount_paise(effective_text) or parse_amount_paise(prompt)
    if not amount_paise:
        return ParsedPaymentInstruction(
            action=action, payee_id=payee_id, payee_display_name=payee_name,
            amount_paise=None, declared_category=default_cat, is_clarification_needed=True,
            clarification_message="What positive amount should I use? No payment was sent.",
        )

    # 4. Determine Category
    declared_category = default_cat
    comb_lower = combined_text.lower()
    if re.search(r"label it as (?:an? )?electricity|call it a utility bill", comb_lower):
        declared_category = "utilities.electricity"
    elif "label it as an investment" in comb_lower or "investment.stocks" in comb_lower:
        declared_category = "investment.stocks"
    elif "investment.crypto" in comb_lower:
        declared_category = "investment.crypto"

    return ParsedPaymentInstruction(
        action=action,
        payee_id=payee_id,
        payee_display_name=payee_name,
        amount_paise=amount_paise,
        declared_category=declared_category,
        reference="INV-DEMO-2026",
        note="AgentGate Demo Payment",
        is_injection_detected=has_injection,
    )
