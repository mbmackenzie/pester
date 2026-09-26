"""Application factory."""

import logging
from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse
from openai import AsyncOpenAI

from pester import admin
from pester.admin.auth import AdminAuth
from pester.admin.queries import AdminQueries
from pester.api import batches, dev, events, health, jobs, personalities, preview
from pester.config import PesterConfig, Settings, load_config
from pester.configstore import ConfigStore
from pester.core.clock import Clock, SystemClock
from pester.core.errors import IdempotencyConflictError, IllegalTransitionError, JobNotFoundError
from pester.delivery.adapters import ChannelServices
from pester.delivery.base import DeliveryChannel
from pester.delivery.manager import ChannelManager
from pester.evaluation.base import EvaluatorRegistry
from pester.live import LiveConfig, SnapshotBuilder
from pester.runtime import Runtime
from pester.state import AppState
from pester.storage.db import Database
from pester.storage.repository import Repository

log = logging.getLogger(__name__)


def create_app(
    settings: Settings | None = None,
    config: PesterConfig | None = None,
    clock: Clock | None = None,
    channels: Sequence[DeliveryChannel] | None = None,
    evaluators: EvaluatorRegistry | None = None,
    llm_client: AsyncOpenAI | None = None,
) -> FastAPI:
    """Build the app.

    Deployment config lives in the database. ``config``, when given, replaces it at startup (tests and
    embedding); otherwise ``PESTER_CONFIG`` seeds an empty database. An explicit ``config`` that can't be put
    into effect (e.g. a broken personality) raises here, before anything is served.
    """
    settings = settings or Settings()
    clock = clock or SystemClock()
    base_dir = settings.config.parent if settings.config else Path.cwd()
    builder = SnapshotBuilder(settings, base_dir, evaluators=evaluators, llm_client=llm_client)
    live = LiveConfig(builder.build(0, config, {}) if config is not None else None)
    state = AppState(settings=settings, clock=clock, live=live)

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        db = await Database.open(settings.database_path)
        repo = Repository(db, clock)
        store = ConfigStore(db, clock)
        await seed_config(store, settings, config)
        stored = await store.latest()
        assert stored is not None
        await live.publish(builder.build(stored.version, stored.config, await store.secrets()))
        manager = ChannelManager(
            ChannelServices(clock, db), channels or (), dev_mock=settings.dev_mode and channels is None
        )
        runtime = Runtime(repo, live, clock, manager, store=store, builder=builder)
        app.state.repo = repo
        app.state.runtime = runtime
        app.state.config_store = store
        app.state.admin = AdminAuth(db, clock)
        app.state.admin_queries = AdminQueries(db)
        await app.state.admin.announce()
        await runtime.recover()
        await runtime.start_channels()
        if settings.run_workers:
            runtime.start_workers()
        try:
            yield
        finally:
            await runtime.stop_workers()
            await runtime.stop_channels()
            await db.close()

    app = FastAPI(title="Pester", version="0.1.0", lifespan=lifespan)
    app.state.pester = state
    for module in (health, jobs, batches, events, preview, personalities):
        app.include_router(module.router)
    if settings.dev_mode:
        app.include_router(dev.router)
    admin.install(app)
    _register_error_handlers(app)
    return app


async def seed_config(store: ConfigStore, settings: Settings, config: PesterConfig | None) -> None:
    """Make sure the database has config, from ``config``, else ``PESTER_CONFIG``, else empty."""
    latest = await store.latest()
    if config is not None:
        if latest is None or latest.config != config:
            await store.save(config, "set by the application at startup")
        return
    if settings.config is not None:
        from_file = load_config(settings.config)
        if latest is None:
            await store.save(from_file, f"imported from {settings.config}")
        elif from_file != latest.config:
            log.warning(
                "%s differs from the config in the database (version %d), which is in effect. The file only "
                "seeds an empty database; run `pester import %s` to apply it.",
                settings.config,
                latest.version,
                settings.config,
            )
        return
    if latest is None:
        await store.save(PesterConfig(), "initial empty config")


def _register_error_handlers(app: FastAPI) -> None:
    @app.exception_handler(JobNotFoundError)
    async def _not_found(request: Request, exc: JobNotFoundError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_404_NOT_FOUND)

    @app.exception_handler(IdempotencyConflictError)
    async def _conflict(request: Request, exc: IdempotencyConflictError) -> JSONResponse:
        return JSONResponse({"detail": str(exc)}, status_code=status.HTTP_409_CONFLICT)

    @app.exception_handler(IllegalTransitionError)
    async def _illegal(request: Request, exc: IllegalTransitionError) -> JSONResponse:
        return JSONResponse(
            {"detail": f"job is {exc.current}; cannot move to {exc.target}"},
            status_code=status.HTTP_409_CONFLICT,
        )
