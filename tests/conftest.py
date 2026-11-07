import sys
from pathlib import Path

import pytest

# The Keiki-style client lives with the Keiki example; the tests use it as is.
sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "examples" / "keiki"))

from fake_api import FakeCloud  # noqa: E402


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
