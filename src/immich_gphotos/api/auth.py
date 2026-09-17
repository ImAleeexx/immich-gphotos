import hashlib
import hmac
import secrets

from fastapi import APIRouter, Form, HTTPException, Request
from fastapi.responses import RedirectResponse

router = APIRouter()

PASSWORD_KEY = "ui_password"
SESSION_COOKIE = "igp_session"
OPEN_PATHS = ("/hooks/immich", "/healthz", "/metrics", "/login", "/static")

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


def requires_setup(services) -> bool:
    return services.settings_repo.get(PASSWORD_KEY) is None


def is_open(path: str) -> bool:
    return any(path.startswith(p) for p in OPEN_PATHS)


@router.post("/login")
def login(request: Request, password: str = Form(...)):
    services = request.app.state.services
    if requires_setup(services):
        # Fresh install: nothing is stored yet, so the password submitted here
        # is the one being set, matching the "Set password" button login.html
        # renders in this state (see task-20 Correction 1).
        services.settings_repo.set(PASSWORD_KEY, hash_password(password))
    else:
        stored = services.settings_repo.get(PASSWORD_KEY)
        if not verify_password(password, stored):
            raise HTTPException(status_code=401, detail="wrong password")

    token = secrets.token_urlsafe(32)
    services.settings_repo.set(SESSION_COOKIE, token)
    response = RedirectResponse("/", status_code=303)
    response.set_cookie(SESSION_COOKIE, token, httponly=True, samesite="lax")
    return response


@router.post("/logout")
def logout(request: Request):
    response = RedirectResponse("/login", status_code=303)
    response.delete_cookie(SESSION_COOKIE)
    return response
