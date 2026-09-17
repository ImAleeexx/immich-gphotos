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
