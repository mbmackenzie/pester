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
from pester.core.clock import Clock, SystemClock
from pester.core.errors import IdempotencyConflictError, IllegalTransitionError, JobNotFoundError
from pester.delivery.base import DeliveryChannel
from pester.delivery.memory import InMemoryChannel
from pester.evaluation.base import Evaluator, EvaluatorRegistry
from pester.evaluation.echo import EchoEvaluator
from pester.evaluation.llm import LLMEvaluator
from pester.evaluation.rule import RuleEvaluator
from pester.llm import make_client
from pester.personality.base import PersonalityServices
from pester.personality.registry import build_registry
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
    """Build the app. Misconfiguration (e.g. a broken personality) raises here, before anything is served."""
    settings = settings or Settings()
    config = config if config is not None else load_config(settings.config)
    llm_client = llm_client or make_client(settings, config.llm)
    base_dir = settings.config.parent if settings.config else Path.cwd()
    state = AppState(
        settings=settings,
        config=config,
        clock=clock or SystemClock(),
        evaluators=evaluators or default_evaluators(settings, config, llm_client),
        personalities=build_registry(config, PersonalityServices(config.llm, llm_client, base_dir)),
    )
    if channels is None:
        channels = [InMemoryChannel(state.clock, name="fake")] if settings.dev_mode else []

    @asynccontextmanager
    async def lifespan(app: FastAPI) -> AsyncGenerator[None]:
        db = await Database.open(settings.database_path)
        repo = Repository(db, state.clock)
        runtime = Runtime(repo, state.config, state.clock, channels, state.evaluators, state.personalities)
        app.state.repo = repo
        app.state.runtime = runtime
        app.state.admin = AdminAuth(db, state.clock)
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


def default_evaluators(
    settings: Settings, config: PesterConfig, llm_client: AsyncOpenAI | None
) -> EvaluatorRegistry:
    by_name: dict[str, Evaluator] = {"rule": RuleEvaluator()}
    if llm_client is not None:
        by_name["llm"] = LLMEvaluator(llm_client, config.llm)
    elif settings.dev_mode:
        log.warning("OPENAI_API_KEY is not set: dev mode answers 'llm' evaluations with the echo evaluator")
        by_name["llm"] = EchoEvaluator()
    if settings.dev_mode:
        by_name["echo"] = EchoEvaluator()
    return EvaluatorRegistry(by_name)


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
