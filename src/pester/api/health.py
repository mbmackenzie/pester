from typing import Any

from fastapi import APIRouter, Response, status

from pester.api.deps import RepoDep, RuntimeDep, StateDep
from pester.runtime import Runtime
from pester.state import AppState
from pester.storage.repository import Repository
from pester.version import VERSION

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up and serving. Used by the container health check."""
    return {"status": "ok", "version": VERSION}


@router.get("/ready")
async def ready(state: StateDep, repo: RepoDep, runtime: RuntimeDep, response: Response) -> dict[str, Any]:
    """Readiness: able to deliver and evaluate. 503 with the failing checks otherwise."""
    checks = await readiness_checks(state, repo, runtime)
    is_ready = all(check["ok"] for check in checks.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if is_ready else "not_ready", "checks": checks}


async def readiness_checks(state: AppState, repo: Repository, runtime: Runtime) -> dict[str, dict[str, Any]]:
    """The checks behind ``/ready``, also shown on the admin dashboard."""
    checks: dict[str, dict[str, Any]] = {}

    try:
        await repo.ping()
        checks["database"] = {"ok": True}
    except Exception as exc:
        checks["database"] = {"ok": False, "error": repr(exc)}

    if state.settings.run_workers:
        workers = runtime.health()
        checks["workers"] = {
            "ok": all(w.running for w in workers.values()),
            **{
                name: {
                    "running": w.running,
                    "last_ok": w.last_ok.isoformat() if w.last_ok else None,
                    "last_error": w.last_error,
                }
                for name, w in workers.items()
            },
        }

    enabled, started = set(runtime.manager.enabled()), runtime.started_channels
    errors = {s.name: s.error for s in runtime.manager.status() if s.error}
    checks["channels"] = {
        "ok": bool(enabled) and enabled <= started,
        "enabled": sorted(enabled),
        "started": sorted(started),
        **({"errors": errors} if errors else {}),
        **({"error": "no delivery channels are enabled"} if not enabled else {}),
    }

    # Informational: jobs naming an unregistered evaluator are rejected at submit, not stuck.
    checks["evaluators"] = {"ok": True, "registered": state.evaluators.names()}
    return checks
