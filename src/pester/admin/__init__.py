"""The LAN-only admin UI, mounted at ``/admin`` (docs/admin-ui.md)."""

from fastapi import FastAPI, Request, Response
from fastapi.staticfiles import StaticFiles

from pester.admin.manage import router  # importing manage registers the config pages on the router
from pester.admin.views import AdminRedirect, redirect


def install(app: FastAPI) -> None:
    app.include_router(router)
    app.mount("/admin/static", StaticFiles(packages=[("pester.admin", "static")]), name="admin-static")

    @app.exception_handler(AdminRedirect)
    async def _redirect(request: Request, exc: AdminRedirect) -> Response:
        return redirect(request, exc.location)
