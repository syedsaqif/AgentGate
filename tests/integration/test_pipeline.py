import pytest

pytestmark = pytest.mark.integration


@pytest.mark.skip(reason="A06 pipeline feature work has not started")
def test_pipeline_placeholder() -> None:
    pass
