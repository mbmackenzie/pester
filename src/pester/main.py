"""Application factory."""

from collections.abc import AsyncGenerator
from contextlib import asynccontextmanager
from dataclasses import dataclass

from fastapi import FastAPI, Request, status
from fastapi.responses import JSONResponse

from pester.api import batches, events, health, jobs
from pester.config import PesterConfig, Settings, load_config
from pester.core.clock import Clock, SystemClock
from pester.core.errors import IdempotencyConflictError, IllegalTransitionError, JobNotFoundError
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
) -> FastAPI:
    settings = settings or Settings()
    state = AppState(
        settings=settings,
        config=config if config is not None else load_config(settings.config),
        clock=clock or SystemClock(),
    )

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        db = await Database.open(settings.database_path)
        app.state.repo = Repository(db, state.clock)
        # Background workers (scheduler, delivery, evaluation) start and stop here.
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(title="Pester", version="0.1.0", lifespan=lifespan)
    app.state.pester = state
    for module in (health, jobs, batches, events):
        app.include_router(module.router)
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
