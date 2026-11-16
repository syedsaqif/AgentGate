import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(reason="C05 approval concurrency work has not started")
def test_approval_race_placeholder() -> None:
    pass
