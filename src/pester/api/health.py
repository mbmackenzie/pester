from typing import Any

from fastapi import APIRouter, Response, status

from pester.api.deps import RepoDep, RuntimeDep, StateDep

router = APIRouter()


@router.get("/health")
async def health() -> dict[str, str]:
    """Liveness: the process is up and serving. Used by the container health check."""
    return {"status": "ok"}


@router.get("/ready")
async def ready(state: StateDep, repo: RepoDep, runtime: RuntimeDep, response: Response) -> dict[str, Any]:
    """Readiness: able to deliver and evaluate. 503 with the failing checks otherwise."""
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

    enabled, started = set(runtime.channels), runtime.started_channels
    checks["channels"] = {
        "ok": bool(enabled) and enabled <= started,
        "enabled": sorted(enabled),
        "started": sorted(started),
        **({"error": "no delivery channels are enabled"} if not enabled else {}),
    }

    # Informational: jobs naming an unregistered evaluator are rejected at submit, not stuck.
    checks["evaluators"] = {"ok": True, "registered": state.evaluators.names()}

    is_ready = all(check["ok"] for check in checks.values())
    if not is_ready:
        response.status_code = status.HTTP_503_SERVICE_UNAVAILABLE
    return {"status": "ready" if is_ready else "not_ready", "checks": checks}
