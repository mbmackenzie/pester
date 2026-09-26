"""Shared request dependencies: app state, repository, and bearer auth."""

import hmac
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Annotated

from fastapi import Depends, HTTPException, Request, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer

from pester.config import ClientConfig, Permission, PesterConfig
from pester.core.tokens import hash_token
from pester.runtime import Runtime
from pester.state import AppState
from pester.storage.repository import Repository

_bearer = HTTPBearer(auto_error=False)


@dataclass(frozen=True)
class Principal:
    client_id: str
    client: ClientConfig


def get_state(request: Request) -> AppState:
    return request.app.state.pester


def get_config(request: Request) -> PesterConfig:
    return get_state(request).config


def get_repo(request: Request) -> Repository:
    return request.app.state.repo


def get_runtime(request: Request) -> Runtime:
    return request.app.state.runtime


def require(permission: Permission) -> Callable[..., Awaitable[Principal]]:
    async def dependency(
        config: Annotated[PesterConfig, Depends(get_config)],
        credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(_bearer)],
    ) -> Principal:
        if credentials is None:
            raise HTTPException(
                status.HTTP_401_UNAUTHORIZED, "missing bearer token", {"WWW-Authenticate": "Bearer"}
            )
        presented = hash_token(credentials.credentials)
        for client_id, client in config.clients.items():
            if hmac.compare_digest(presented, client.token_hash):
                if permission not in client.permissions:
                    raise HTTPException(status.HTTP_403_FORBIDDEN, f"client lacks {permission} permission")
                return Principal(client_id, client)
        raise HTTPException(
            status.HTTP_401_UNAUTHORIZED, "invalid bearer token", {"WWW-Authenticate": "Bearer"}
        )

    return dependency


StateDep = Annotated[AppState, Depends(get_state)]
ConfigDep = Annotated[PesterConfig, Depends(get_config)]
RepoDep = Annotated[Repository, Depends(get_repo)]
RuntimeDep = Annotated[Runtime, Depends(get_runtime)]
SubmitPrincipal = Annotated[Principal, Depends(require(Permission.SUBMIT_JOBS))]
ReadPrincipal = Annotated[Principal, Depends(require(Permission.READ_EVENTS))]
PreviewPrincipal = Annotated[Principal, Depends(require(Permission.PREVIEW))]
