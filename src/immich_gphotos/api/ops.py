from fastapi import APIRouter, Request
from fastapi.responses import PlainTextResponse

router = APIRouter()


@router.get("/healthz")
def healthz() -> dict:
    return {"status": "ok"}


@router.get("/metrics", response_class=PlainTextResponse)
def metrics(request: Request) -> str:
    services = request.app.state.services
    counts = services.assets.counts_by_state()
    paused = 1 if getattr(services.runtime, "paused_reason", None) else 0

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
