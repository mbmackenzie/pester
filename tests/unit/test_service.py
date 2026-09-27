from pathlib import Path
from typing import Any

import pytest
import yaml

from pester.config import PesterConfig
from pester.configstore import ConfigStore
from pester.core.clock import FakeClock
from pester.core.tokens import verify_token
from pester.live import SnapshotBuilder
from pester.pairing import PairingStore
from pester.service import AdminError, AdminService
from pester.storage.db import Database
from tests.conftest import make_settings


@pytest.fixture
async def db(tmp_path: Path) -> Any:
    database = await Database.open(tmp_path / "pester.sqlite")
    yield database
    await database.close()


@pytest.fixture
def store(db: Database, clock: FakeClock) -> ConfigStore:
    return ConfigStore(db, clock)


@pytest.fixture
def service(tmp_path: Path, db: Database, store: ConfigStore, clock: FakeClock) -> AdminService:
    builder = SnapshotBuilder(make_settings(tmp_path, dev_mode=False), tmp_path)

    def validate(config: PesterConfig, secrets: Any) -> None:
        builder.build(0, config, secrets)

    return AdminService(store, PairingStore(db, clock), validate)


async def config_of(service: AdminService) -> PesterConfig:
    return (await service.current()).config


# ---- Clients --------------------------------------------------------------------------------------------


async def test_create_client_stores_only_the_hash(service: AdminService) -> None:
    await service.create_recipient("kate")
    created = await service.create_client("study", ["submit_jobs", "read_events"], ["kate"])
    client = (await config_of(service)).clients["study"]
    assert verify_token(created.token, client.token_hash)
    assert created.token not in (await service.export_yaml())
    assert sorted(client.permissions) == ["read_events", "submit_jobs"]
    assert client.recipients == {"kate"}


@pytest.mark.parametrize(
    ("client_id", "permissions", "recipients", "message"),
    [
        ("bad id!", ["submit_jobs"], [], "must be letters"),
        ("ok", ["launch_missiles"], [], "unknown permission"),
        ("ok", ["submit_jobs"], ["nobody"], "unknown recipients"),
    ],
)
async def test_create_client_rejects_bad_input(
    service: AdminService, client_id: str, permissions: list[str], recipients: list[str], message: str
) -> None:
    with pytest.raises(AdminError, match=message):
        await service.create_client(client_id, permissions, recipients)


async def test_client_lifecycle(service: AdminService) -> None:
    await service.create_recipient("kate")
    first = await service.create_client("study", ["submit_jobs"])
    with pytest.raises(AdminError, match="already exists"):
        await service.create_client("study", ["submit_jobs"])
    await service.update_client("study", recipients=["kate"], permissions=["read_events"])
    assert (await config_of(service)).clients["study"].recipients == {"kate"}

    rotated = await service.rotate_client_token("study")
    hashed = (await config_of(service)).clients["study"].token_hash
    assert verify_token(rotated.token, hashed)
    assert not verify_token(first.token, hashed)

    await service.delete_client("study")
    assert "study" not in (await config_of(service)).clients
    with pytest.raises(AdminError, match="there is no client"):
        await service.delete_client("study")


# ---- Recipients -----------------------------------------------------------------------------------------


async def test_recipients(service: AdminService) -> None:
    await service.create_client("study", ["submit_jobs"])
    await service.create_recipient(
        "kate", timezone="America/New_York", quiet_hours="22:00-09:00", clients=["study"]
    )
    kate = (await config_of(service)).recipients["kate"]
    assert kate.timezone == "America/New_York"
    assert kate.quiet_hours is not None and f"{kate.quiet_hours.start:%H:%M}" == "22:00"
    assert (await config_of(service)).clients["study"].recipients == {"kate"}

    await service.update_recipient("kate", quiet_hours=None)
    assert (await config_of(service)).recipients["kate"].quiet_hours is None
    await service.update_recipient("kate", timezone="Europe/London")  # quiet hours left alone
    assert (await config_of(service)).recipients["kate"].timezone == "Europe/London"

    await service.link_channel("kate", "mock", {"address": "kate"})
    assert (await config_of(service)).recipients["kate"].channels == {"mock": {"address": "kate"}}
    await service.unlink_channel("kate", "mock")

    await service.delete_recipient("kate")  # also removed from every client
    config = await config_of(service)
    assert "kate" not in config.recipients
    assert config.clients["study"].recipients == frozenset()


@pytest.mark.parametrize(
    ("kwargs", "message"),
    [
        ({"timezone": "Mars/Olympus"}, "unknown timezone"),
        ({"quiet_hours": "late"}, "should look like 22:00-09:00"),
    ],
)
async def test_recipient_input_is_checked(
    service: AdminService, kwargs: dict[str, Any], message: str
) -> None:
    with pytest.raises(AdminError, match=message):
        await service.create_recipient("kate", **kwargs)


# ---- Channels -------------------------------------------------------------------------------------------

TOKEN_TYPE = "tests.e2e.test_channels:TokenAdapter"


async def test_channels(service: AdminService, store: ConfigStore) -> None:
    await service.add_channel("mock", "mock", description="try it out")
    assert (await config_of(service)).channels["mock"].description == "try it out"
    with pytest.raises(AdminError, match="already exists"):
        await service.add_channel("mock", "mock")
    with pytest.raises(AdminError, match="unknown channel type"):
        await service.add_channel("tg", "telegraph")
    with pytest.raises(AdminError, match="Extra inputs"):
        await service.add_channel("tg", "mock", {"colour": "blue"})

    await service.update_channel("mock", enabled=False, accept_pairing=False)
    mock = (await config_of(service)).channels["mock"]
    assert (mock.enabled, mock.accept_pairing, mock.description) == (False, False, "try it out")


async def test_channel_secrets_never_enter_config(service: AdminService, store: ConfigStore) -> None:
    await service.add_channel("tg", TOKEN_TYPE, {"token": "given-inline", "greeting": "yo"})
    assert "given-inline" not in (await config_of(service)).model_dump_json()
    assert (await store.secrets())["channel.tg.token"] == "given-inline"

    await service.set_channel_secret("tg", "token", "replaced")
    assert (await store.secrets())["channel.tg.token"] == "replaced"
    with pytest.raises(AdminError, match="isn't a secret"):
        await service.set_channel_secret("tg", "greeting", "hello")

    await service.remove_channel("tg")  # its secrets go with it
    assert await store.secrets() == {}


# ---- Personalities, settings ----------------------------------------------------------------------------


async def test_personalities(service: AdminService) -> None:
    await service.set_personality(
        "fern", "template", {"feedback": "🌿 {{ feedback_facts }}"}, description="A fern"
    )
    assert (await config_of(service)).personalities["fern"].description == "A fern"
    with pytest.raises(AdminError, match="fern"):  # validated by building it: bad template syntax
        await service.set_personality("fern", "template", {"feedback": "{{ unclosed"})
    await service.set_default_personality("fern")
    with pytest.raises(AdminError, match="is the default personality"):
        await service.delete_personality("fern")
    await service.set_default_personality("default")
    await service.delete_personality("fern")


async def test_settings(service: AdminService, store: ConfigStore) -> None:
    await service.update_settings("scheduler", {"max_messages_per_day": 6, "quiet_hours": None})
    config = await config_of(service)
    assert config.scheduler.max_messages_per_day == 6
    assert config.scheduler.quiet_hours is None
    with pytest.raises(AdminError, match="max_messages_per_day"):
        await service.update_settings("scheduler", {"max_messages_per_day": 0})
    with pytest.raises(AdminError, match="unknown settings section"):
        await service.update_settings("vibes", {"x": 1})

    await service.set_llm_key("sk-test")
    assert (await store.secrets())["llm.api_key"] == "sk-test"
    await service.set_llm_key(None)
    assert await store.secrets() == {}


# ---- Import / export, concurrency -----------------------------------------------------------------------


async def test_export_then_import_round_trips(service: AdminService, store: ConfigStore) -> None:
    await service.create_recipient("kate", quiet_hours="22:00-09:00")
    await service.create_client("study", ["submit_jobs"], ["kate"])
    await service.add_channel("tg", TOKEN_TYPE, {"token": "secret-value"})
    exported = await service.export_yaml()
    assert "secret-value" not in exported
    before = await config_of(service)

    await service.import_yaml("recipients: {}\n", "wipe.yaml")
    assert (await config_of(service)).recipients == {}
    await service.import_yaml(exported, "backup.yaml")
    assert await config_of(service) == before
    assert (await store.history())[0].comment == "imported from backup.yaml"


async def test_import_moves_secrets_out_of_the_file(service: AdminService, store: ConfigStore) -> None:
    text = yaml.safe_dump({"channels": {"tg": {"type": TOKEN_TYPE, "token": "from-file", "greeting": "hey"}}})
    await service.import_yaml(text, "prod.yaml")
    assert (await config_of(service)).channels["tg"].options == {"greeting": "hey"}
    assert (await store.secrets())["channel.tg.token"] == "from-file"


async def test_import_rejects_invalid_files(service: AdminService) -> None:
    with pytest.raises(AdminError, match="isn't valid YAML"):
        await service.import_yaml("a: [", "bad.yaml")
    with pytest.raises(AdminError, match="unknown timezone"):
        await service.import_yaml("recipients: {kate: {timezone: Mars/Olympus}}", "bad.yaml")


async def test_a_concurrent_change_is_not_lost(service: AdminService, store: ConfigStore) -> None:
    await service.create_recipient("kate")
    calls = 0

    def edit(data: dict[str, Any]) -> None:
        nonlocal calls
        calls += 1
        data["recipients"]["sam"] = {}

    original_save = store.save

    async def racing_save(*args: Any, **kwargs: Any) -> int:
        if calls == 1:  # someone else saves between our read and our write
            latest = await store.latest()
            assert latest is not None
            data = latest.config.model_dump(mode="json")
            data["recipients"]["alex"] = {}
            await original_save(PesterConfig.model_validate(data), "someone else")
        return await original_save(*args, **kwargs)

    store.save = racing_save  # type: ignore[method-assign]
    await service.update("add sam", edit)
    assert calls == 2  # retried on top of the other change
    assert set((await config_of(service)).recipients) == {"kate", "sam", "alex"}
