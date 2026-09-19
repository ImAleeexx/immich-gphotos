from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request) -> str:
    # RULING R1: `/metrics` is in `auth.OPEN_PATHS` (Prometheus scrapes it
    # unauthenticated), so `require_session` returns before ever setting
    # `request.state.services` -- reading that attribute here would 500 on
    # every single scrape. Resolve straight from the registry instead, and
    # tolerate there being no account at all: a fresh install nobody has
    # added an account to yet must still answer an unauthenticated,
    # nobody's-watching-it scrape with zero/absent values, never a 500.
    registry = request.app.state.accounts
    account = registry.default()
    counts = account.services.assets.counts_by_state() if account is not None else {}
    paused = 1 if account is not None and getattr(account.services.runtime, "paused_reason", None) else 0

    lines = [
        "# HELP immich_gphotos_assets_total Assets by sync state.",
        "# TYPE immich_gphotos_assets_total gauge",
    ]
    for state, count in sorted(counts.items()):
        lines.append(f'immich_gphotos_assets_total{{state="{state}"}} {count}')
    lines += [
        "# HELP immich_gphotos_paused Whether transfer is halted awaiting a human.",
        "# TYPE immich_gphotos_paused gauge",
        f"immich_gphotos_paused {paused}",
    ]
    return "\n".join(lines) + "\n"
