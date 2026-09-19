"""Keys under which credentials and identifiers live in the `setting` table.

Centralized so the writers (the setup wizard's API routes) and the reader
(`main.build_services`) can never drift apart — that drift is exactly the bug
this file exists to prevent. Nothing else should hardcode these strings.
"""

SECRET_KEY = "webhook_secret"
IMMICH_URL_KEY = "immich_url"
IMMICH_KEY_KEY = "immich_api_key"
GOOGLE_AUTH_KEY = "google_auth_data"
WORKFLOW_ID_KEY = "workflow_id"

# The admin password and live session token. Defined here rather than in
# `api.auth` so that `accounts.migrate` (which runs on the boot path, before
# any web framework is touched) can read them without importing `api` -- that
# import would drag FastAPI in and invert the dependency between the two
# packages. `api.auth` re-exports both names for its existing importers.
PASSWORD_KEY = "ui_password"
SESSION_COOKIE = "igp_session"

# The JSON blob of tunables the API persists. It lives in BOTH databases: the
# account's copy holds that account's half (quality, albums_enabled,
# deletions_enabled), the control copy holds the half that is global because
# the resource is (bandwidth is one uplink, worker_threads is one machine).
SETTINGS_KEY = "settings"
GLOBAL_SETTING_KEYS = frozenset({"bandwidth_bytes_per_second", "worker_threads"})

# Which account the pre-multi-account webhook path `/hooks/immich` belongs to.
# Set once by the migration and never changed: an existing install already has
# a workflow registered in Immich against that bare path, and repointing or
# dropping it stops that person's backups with nothing in the UI to show it.
LEGACY_WEBHOOK_ACCOUNT_KEY = "legacy_webhook_account"

# Bounds shared between the API's `SettingsPatch` validation
# (`api.routes`) and `accounts.build._merged_settings`, which validates a
# stored settings row against these same bounds so a hand-edited database
# row can never apply a value the API itself would reject. Defined here
# rather than in `api.routes`, for the same reason `PASSWORD_KEY` is: the
# `accounts` package runs on the boot path and must never import the API
# package, which would drag FastAPI in through it. `api.routes` re-exports
# all three for its existing importers.
MIN_WORKER_THREADS = 1
MAX_WORKER_THREADS = 16

# 0 passes `ge=0` and looks like a reasonable way to type "no cap" (the UI's
# own "blank = unlimited" hint invites exactly that), but TokenBucket must
# reject or ignore a non-positive rate rather than run with one -- so it can
# never be a valid *cap* in the first place. blank/omitted (None) is still
# how "unlimited" is actually spelled.
MIN_BANDWIDTH_BYTES_PER_SECOND = 65536
