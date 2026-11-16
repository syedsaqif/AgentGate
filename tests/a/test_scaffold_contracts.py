import pytest
from pydantic import ValidationError

from contracts.core_models import ActionParams

pytestmark = [pytest.mark.scaffold, pytest.mark.contract]


def test_money_is_strict_integer_paise() -> None:
    with pytest.raises(ValidationError):
        ActionParams(from_account="ACC-001", amount_paise=1.5)
    with pytest.raises(ValidationError):
        ActionParams(from_account="ACC-001", amount_paise=True)


def test_currency_is_inr_only() -> None:
    with pytest.raises(ValidationError):
        ActionParams(from_account="ACC-001", currency="USD")
