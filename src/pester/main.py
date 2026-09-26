"""Application factory."""

from collections.abc import AsyncGenerator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from pester.api import batches, dev, events, health, jobs
from pester.config import PesterConfig, Settings, load_config
from pester.core.clock import Clock, SystemClock
from pester.core.errors import IdempotencyConflictError, IllegalTransitionError, JobNotFoundError
from pester.delivery.base import DeliveryChannel
from pester.delivery.memory import InMemoryChannel
from pester.evaluation.base import EvaluatorRegistry
from pester.evaluation.echo import EchoEvaluator
from pester.runtime import Runtime
from pester.storage.db import Database
from pester.storage.repository import Repository


@dataclass(frozen=True)
class AppState:
    settings: Settings
    config: PesterConfig
    clock: Clock


def create_app(
    settings: Settings | None = None,
    config: PesterConfig | None = None,
    clock: Clock | None = None,
    channels: Sequence[DeliveryChannel] | None = None,
    evaluators: EvaluatorRegistry | None = None,
) -> FastAPI:
    settings = settings or Settings()
    state = AppState(
        settings=settings,
        config=config if config is not None else load_config(settings.config),
        clock=clock or SystemClock(),
    )
    if channels is None:
        channels = [InMemoryChannel(state.clock, name="fake")] if settings.dev_mode else []
    # The LLM evaluator arrives in M3; until then every job is echoed.
    evaluators = evaluators or EvaluatorRegistry(default=EchoEvaluator())

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        db = await Database.open(settings.database_path)
        repo = Repository(db, state.clock)
        runtime = Runtime(repo, state.config, state.clock, channels, evaluators)
        app.state.repo = repo
        app.state.runtime = runtime
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
    for module in (health, jobs, batches, events):
        app.include_router(module.router)
    if settings.dev_mode:
        app.include_router(dev.router)
    _register_error_handlers(app)
    return app


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
