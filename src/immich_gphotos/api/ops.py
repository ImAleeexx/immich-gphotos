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
    #
    # FINDING I1: every series carries an `account` label, and the loop is
    # over `registry.all()`, not `registry.default()`. The single-account
    # shape this used to emit made accounts 2..N simply invisible -- and
    # `immich_gphotos_paused` read 0 while an account sat in AUTH_INVALID,
    # which is this project's worst failure shape (backups silently stopped,
    # everything else still looks healthy) appearing as "fine" in the one
    # surface that exists to catch it.
    #
    # The label is the opaque account id, NEVER `account.label`: the label is
    # user-supplied free text and this endpoint is unauthenticated by design,
    # so putting it here would both leak what someone named their account to
    # anyone who can reach the port and hand them a quote or newline to break
    # the exposition format with. Ids come from `new_account_id()` (hex) or,
    # at worst, `migrate._SAFE_ACCOUNT_ID` ([A-Za-z0-9_-]), so nothing in an
    # id can escape the label's quotes.
    registry = request.app.state.accounts

    lines = [
        "# HELP immich_gphotos_assets_total Assets by sync state.",
        "# TYPE immich_gphotos_assets_total gauge",
    ]
    paused_lines = [
        "# HELP immich_gphotos_paused Whether transfer is halted awaiting a human.",
        "# TYPE immich_gphotos_paused gauge",
    ]
    for account in registry.all():
        services = account.services
        for state, count in sorted(services.assets.counts_by_state().items()):
            lines.append(f'immich_gphotos_assets_total{{account="{account.id}",state="{state}"}} {count}')
        paused = 1 if getattr(services.runtime, "paused_reason", None) else 0
        paused_lines.append(f'immich_gphotos_paused{{account="{account.id}"}} {paused}')

    # A zero-account install emits the HELP/TYPE headers and no series at
    # all, which is a valid, scrapeable exposition -- the same "200 with
    # nothing to report" the single-account version answered with.
    return "\n".join(lines + paused_lines) + "\n"
