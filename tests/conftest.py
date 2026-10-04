import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from fake_backend import FakeBackend  # noqa: E402
from minions.config import Config  # noqa: E402


@pytest.fixture
def fake():
    b = FakeBackend()
    yield b
    b.close()


@pytest.fixture
def cfg(fake):
    return Config(backend="lmstudio", base_url=fake.url, model="test-model", timeout=5)
