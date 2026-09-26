from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from pester.core.clock import FakeClock
from pester.storage.db import Database
from pester.storage.repository import Repository


@pytest.fixture
async def db(tmp_path: Path) -> AsyncIterator[Database]:
    database = await Database.open(tmp_path / "pester.sqlite")
    yield database
    await database.close()


@pytest.fixture
def repo(db: Database, clock: FakeClock) -> Repository:
    return Repository(db, clock)
