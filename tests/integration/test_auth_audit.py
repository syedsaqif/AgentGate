import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(reason="B08 real auth/audit integration work has not started")
def test_auth_audit_placeholder() -> None:
    pass
