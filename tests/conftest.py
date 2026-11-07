import pytest

from fake_api import FakeCloud


@pytest.fixture
def anyio_backend():
    return "asyncio"


@pytest.fixture
def api():
    fake = FakeCloud().start()
    try:
        yield fake
    finally:
        fake.stop()
