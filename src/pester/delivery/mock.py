"""The ``mock`` channel adapter: a messenger inside Pester, used from the admin UI's Messenger page.

It behaves like a real chat (sequential message ids per conversation, reply threading, option buttons) and
keeps its conversations in SQLite, so they survive restarts and message ids are never reused.
"""

from typing import Any

from pydantic import BaseModel, ConfigDict

from pester.core.clock import Clock
from pester.delivery.adapters import ChannelServices
from pester.delivery.base import InboundHandler
from pester.delivery.memory import ChatMessage, InMemoryChannel
from pester.storage.db import Database


class MockOptions(BaseModel):
    model_config = ConfigDict(extra="forbid")


class MockAdapter:
    description = "A built-in messenger for trying Pester out, used from the admin UI's Messenger page"
    options_model = MockOptions

    def create(self, name: str, options: Any, services: ChannelServices) -> "MockChannel":
        return MockChannel(services.clock, services.db, name)


class MockChannel(InMemoryChannel):
    def __init__(self, clock: Clock, db: Database, name: str) -> None:
        super().__init__(clock, name)
        self._db = db

    async def start(self, on_inbound: InboundHandler) -> None:
        async with self._db.read() as conn:
            rows = await conn.execute_fetchall(
                "SELECT address, message FROM mock_messages WHERE channel = ? ORDER BY address, id",
                (self.name,),
            )
        self._chats.clear()
        self._last_id.clear()
        for row in rows:
            message = ChatMessage.model_validate_json(row["message"])
            self._chats[row["address"]].append(message)
            self._last_id[row["address"]] = message.id
        await super().start(on_inbound)

    async def _persist(self, address: str, message: ChatMessage) -> None:
        async with self._db.transaction() as conn:
            await conn.execute(
                "INSERT INTO mock_messages (channel, address, id, message) VALUES (?, ?, ?, ?)",
                (self.name, address, message.id, message.model_dump_json()),
            )
