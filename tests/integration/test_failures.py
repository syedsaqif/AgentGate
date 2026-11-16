import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(reason="A06 failure-path feature work has not started")
def test_failure_placeholder() -> None:
    pass
