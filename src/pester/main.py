"""Application factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI

from pester.api import health
from pester.config import PesterConfig, Settings, load_config
from pester.core.clock import Clock, SystemClock


@dataclass(frozen=True)
class AppState:
    settings: Settings
    config: PesterConfig
    clock: Clock


def create_app(
    settings: Settings | None = None,
    config: PesterConfig | None = None,
    clock: Clock | None = None,
) -> FastAPI:
    settings = settings or Settings()
    state = AppState(
        settings=settings,
        config=config if config is not None else load_config(settings.config),
        clock=clock or SystemClock(),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        # Background workers (scheduler, delivery, evaluation) start and stop here.
        yield

    app = FastAPI(title="Pester", version="0.1.0", lifespan=lifespan)
    app.state.pester = state
    app.include_router(health.router)
    return app
