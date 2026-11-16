import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(reason="A05 atomic reservation feature work has not started")
def test_budget_race_placeholder() -> None:
    pass
