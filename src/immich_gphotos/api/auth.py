import hashlib
import hmac
import secrets
from typing import TYPE_CHECKING

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

# Re-exported so existing importers of this module (including tests/api/*)
# keep working untouched. The values live in storage_keys so that
# accounts.migrate, which runs on the boot path, can read them without
# importing this module and dragging FastAPI in through it.
from immich_gphotos.storage_keys import PASSWORD_KEY, SESSION_COOKIE  # noqa: F401

if TYPE_CHECKING:
    from immich_gphotos.accounts.registry import AccountRegistry

router = APIRouter()

OPEN_PATHS = frozenset({"/hooks/immich", "/healthz", "/metrics", "/login"})

# Served to unauthenticated browsers on purpose: the login page needs its own
# stylesheet, fonts and icon to render, and `/login` being open while its
# assets were not is what left that page unstyled. Scoped to this one prefix
# rather than a general `startswith` rule -- everything under it is a static
# file with no user data in it, and StaticFiles resolves paths against the
# mounted directory, so the prefix cannot be walked out of.
STATIC_PREFIX = "/static/"

_ITERATIONS = 200_000


def hash_password(raw: str) -> str:
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode(), salt, _ITERATIONS)
    return f"{salt.hex()}${digest.hex()}"


def verify_password(raw: str, stored: str) -> bool:
    try:
        salt_hex, digest_hex = stored.split("$", 1)
    except ValueError:
        return False
    digest = hashlib.pbkdf2_hmac("sha256", raw.encode(), bytes.fromhex(salt_hex), _ITERATIONS)
    return hmac.compare_digest(digest.hex(), digest_hex)


def requires_setup(registry: "AccountRegistry") -> bool:
    # The admin password lives in the control database, not any account's --
    # see the module docstring on `accounts.control`. Reading it off the
    # registry rather than a `Services` is what lets this be true (and the
    # login page render) before a single account exists.
    return registry.settings.get(PASSWORD_KEY) is None


def is_open(path: str) -> bool:
    return path in OPEN_PATHS or path.startswith(STATIC_PREFIX)


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    registry = request.app.state.accounts
    if requires_setup(registry):
        # Fresh install: nothing is stored yet, so the password submitted here
        # is the one being set, matching the "Set password" button login.html
        # renders in this state (see task-20 Correction 1).
        registry.settings.set(PASSWORD_KEY, hash_password(password))
    else:
        stored = registry.settings.get(PASSWORD_KEY)
        if not verify_password(password, stored):
            raise HTTPException(status_code=401, detail="wrong password")

    token = secrets.token_urlsafe(32)
    registry.settings.set(SESSION_COOKIE, token)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@router.post("/logout")
def logout(request: Request):
    registry = request.app.state.accounts
    # The middleware trusts registry.settings, not the browser, as the source
    # of truth for whether a session is live: clearing only the cookie would
    # leave a captured cookie value valid forever.
    registry.settings.set(SESSION_COOKIE, None)
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
