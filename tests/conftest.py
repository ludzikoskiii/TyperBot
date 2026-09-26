from __future__ import annotations

from datetime import datetime, timezone

import pytest

from typerbot.config.secrets import KEYED_SOURCES, MemorySecretStore
from typerbot.data.db import Database

NOW = datetime(2026, 9, 26, 12, 0, tzinfo=timezone.utc)


class FakeClock:
    def __init__(self, start: float = NOW.timestamp()):
        self.t = start
        self.slept: list[float] = []

    def __call__(self) -> float:
        return self.t

    def sleep(self, seconds: float) -> None:
        self.slept.append(seconds)
        self.t += seconds

    def advance(self, seconds: float) -> None:
        self.t += seconds


@pytest.fixture
def db(tmp_path):
    database = Database(tmp_path / "test.db")
    yield database
    database.close()


@pytest.fixture
def clock():
    return FakeClock()


@pytest.fixture
def secrets():
    return MemorySecretStore({s: f"key-{s}" for s in KEYED_SOURCES})
