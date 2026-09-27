import pytest

from pester.config import PesterConfig
from pester.configstore import ConfigConflictError, ConfigStore
from pester.core.clock import FakeClock
from pester.storage.db import Database


@pytest.fixture
def store(db: Database, clock: FakeClock) -> ConfigStore:
    return ConfigStore(db, clock)


def with_recipient(name: str) -> PesterConfig:
    return PesterConfig.model_validate({"recipients": {name: {"timezone": "Europe/London"}}})


async def test_empty_store(store: ConfigStore) -> None:
    assert await store.latest() is None
    assert await store.latest_version() == 0
    assert await store.secrets() == {}


async def test_each_save_is_a_new_version(store: ConfigStore) -> None:
    v1 = await store.save(with_recipient("kate"), "first")
    v2 = await store.save(with_recipient("sam"), "second")
    assert (v1, v2) == (1, 2)
    latest = await store.latest()
    assert latest is not None
    assert latest.version == 2
    assert set(latest.config.recipients) == {"sam"}
    assert [c.comment for c in await store.history()] == ["second", "first"]


async def test_config_round_trips_exactly(store: ConfigStore) -> None:
    config = PesterConfig.model_validate(
        {
            "recipients": {
                "kate": {"quiet_hours": {"start": "22:00", "end": "09:00"}, "channels": {"x": {"a": 1}}}
            },
            "personalities": {"fern": {"type": "template", "feedback": "{{ feedback_facts }}"}},
            "scheduler": {"quiet_hours": None, "jitter_minutes": 5},
        }
    )
    await store.save(config, "round trip")
    latest = await store.latest()
    assert latest is not None
    assert latest.config == config


async def test_optimistic_save_detects_a_concurrent_change(store: ConfigStore) -> None:
    await store.save(with_recipient("kate"), "first")
    await store.save(with_recipient("sam"), "someone else", expected_version=1)
    with pytest.raises(ConfigConflictError):
        await store.save(with_recipient("alex"), "stale", expected_version=1)
    assert await store.latest_version() == 2


async def test_secrets_are_stored_apart_and_bump_the_version(store: ConfigStore) -> None:
    config = with_recipient("kate")
    await store.save(config, "first")
    await store.save(config, "set key", secrets={"llm.api_key": "sk-1"})
    assert await store.secrets() == {"llm.api_key": "sk-1"}
    assert await store.latest_version() == 2  # workers reload and pick up the new key
    await store.save(config, "replace", secrets={"llm.api_key": "sk-2"})
    assert await store.secrets() == {"llm.api_key": "sk-2"}
    await store.save(config, "remove", secrets={"llm.api_key": None})
    assert await store.secrets() == {}


async def test_a_failed_conflict_check_changes_nothing(store: ConfigStore) -> None:
    await store.save(with_recipient("kate"), "first")
    with pytest.raises(ConfigConflictError):
        await store.save(with_recipient("sam"), "stale", expected_version=0, secrets={"s": "v"})
    assert await store.secrets() == {}
    assert await store.latest_version() == 1
