# immich-gphotos

Mirror your Immich library to Google Photos, automatically.

Driven by Immich's native workflow webhooks, with a reconciler that guarantees
nothing is missed. Every asset's SHA-1 is checked against Google **before** any
bytes move, so an existing library backfills without re-uploading what Google
already has.

> **Read this first.** This project uploads through
> [gpmc](https://github.com/xob0t/gpmc), a **reverse-engineered** Google Photos
> mobile API. It is unofficial, it can break without notice, and using it
> carries some risk to your Google account. This project is **not affiliated
> with** Google or with Immich. Use it as an additional copy, never as your only
> backup.

## Requirements

- Immich 3.0 or newer for event-driven backup (older versions fall back to
  polling automatically)
- An Immich API key
- Google `auth_data`, extracted once from an Android device — the in-app setup
  wizard walks through this

## Quick start

```yaml
# docker-compose.yml
services:
  immich-gphotos:
    image: ghcr.io/imaleeexx/immich-gphotos:latest
    container_name: immich-gphotos
    restart: unless-stopped
    ports:
      - "8080:8080"
    environment:
      IGP_DATA_DIR: /data
      IGP_LOG_LEVEL: INFO
    volumes:
      - ./data:/data
      # Optional but recommended when Immich runs on this host: see "Reading
      # originals directly" below.
      - /path/to/immich/library:/usr/src/app/upload:ro
```

```
docker compose up -d
```

Then open `http://<host>:8080` and follow the setup wizard. No credential is
ever passed as an environment variable or written into the compose file — the
wizard is the only place they're entered, and they're stored in the SQLite
database under `IGP_DATA_DIR`, not in this file.

## The setup wizard

Four screens, each refusing to advance until it has proven the thing actually
works, not just that something was typed:

1. **Immich.** Paste the server URL and an API key. The wizard calls the real
   API to check the server version and the key's granted permissions, and
   lists any that are missing by name.
2. **Google.** Paste `auth_data`. The wizard proves it's valid with a real
   authenticated lookup (a hash that cannot exist), never by just accepting
   the string.
3. **Workflow.** One button registers an Immich workflow — trigger
   `AssetCreate`, step `immich-plugin-core#webhook` — with a generated shared
   secret. On Immich versions without the workflow system this step is
   skipped and the service instead polls on a reconcile interval; the
   dashboard states which mode is active.
4. **Options.** Quality, schedule window, bandwidth cap, content filters,
   whether to mirror albums, and whether to start backfilling existing assets
   immediately.

## Settings

The Settings page (and `PUT /api/settings`) exposes:

| Setting | Effect |
|---|---|
| `quality` | `original`, `saver`, or `quota` — which Google Photos upload tier to use. |
| `albums_enabled` | Reproduce Immich albums as Google Photos albums. |
| `deletions_enabled` | Propagate Immich deletions to Google Photos (see below). Off by default. |
| `worker_threads` | How many uploads run concurrently. |
| `bandwidth_bytes_per_second` | Cap on upload throughput; unset for none. |

Schedule window, content filters and retry behavior are configured with
sensible defaults and are not yet exposed for editing in this release.

## Deletions

Off by default, and opt-in through an explicit toggle. When enabled, the
reconciler additionally detects assets that were trashed in Immich and trashes
the corresponding item in Google Photos — using Google's own trash and its
60-day recovery window, never a permanent delete.

**Safety limit.** If a single pass would trash more than 10% of the synced
library, or more than 500 assets, whichever is reached first, the service
refuses to execute that pass and raises an alert in the UI instead. This
guards against an Immich database restored from an old backup being read as
"everything was deleted." Neither threshold can be disabled.

## Reading originals directly

`originalPath` arrives in every webhook payload and reconcile page. If the
container can read that path itself, the byte resolver hands it straight to
the uploader: no copy, no scratch space, and no round trip through the Immich
API to download the file first. This only works if the path inside this
container is the *same* path Immich's own container sees — that's why the
compose snippet mounts the library read-only at `/usr/src/app/upload`, which
is Immich's default upload location. Match that to wherever your Immich
container actually mounts its library.

When the mount isn't present, or the file isn't readable, this falls back
automatically to downloading the original through the Immich API into a
scratch directory, uploading it, then deleting the scratch copy. Nothing
breaks without the mount — it's a performance path, not a requirement.

Set `IGP_ALLOW_DIRECT_READS=false` to force the API-download path even when
the mount is present — useful for confirming the fallback still works, or if
the mounted path is present but not trustworthy for some reason (e.g. it's a
stale snapshot rather than Immich's live library).

## Environment variables

Everything below is optional and has a working default; none of them ever
holds a credential — Immich and Google credentials live only in the database
under `IGP_DATA_DIR`, entered through the setup wizard.

| Variable | Default | Meaning |
|---|---|---|
| `IGP_DATA_DIR` | `/data` | Where the SQLite database (and, unless overridden, the scratch directory) live. |
| `IGP_LOG_LEVEL` | `INFO` | Log verbosity. |
| `IGP_HOST` | `0.0.0.0` | Address the web server binds to. |
| `IGP_PORT` | `8080` | Port the web server listens on. |
| `IGP_ALLOW_DIRECT_READS` | `true` | Set to `false` to always download originals through the Immich API instead of reading a mounted library directly (see above). |
| `IGP_SCRATCH_DIR` | `<IGP_DATA_DIR>/scratch` | Where originals are staged when the direct-read path isn't used. |

## Monitoring

- `GET /healthz` — liveness only, unauthenticated, used by the image's own
  `HEALTHCHECK`.
- `GET /metrics` — Prometheus text format, unauthenticated. Exposes
  `immich_gphotos_assets_total{state=...}` (a gauge per sync state) and
  `immich_gphotos_paused` (1 while transfer is halted awaiting a human, e.g.
  because Google credentials expired).
- The dashboard (`/`) shows the same counts live over server-sent events, plus
  backfill progress and whether the fast, direct-read path is active.

## What has actually been verified

This has been run end to end against a real Immich 3.2.2 instance and a real
Google Photos account:

- Immich's `AssetCreate` webhook fires and its payload parses correctly.
- The asset checksum Immich reports is the SHA-1 of the original file, which
  is what Google Photos deduplicates on.
- A hash Google Photos already holds is recognized without uploading anything.
- Upload, dedup lookup, and trash all work against the real APIs.

**Not yet verified against real devices:** motion photos. Samsung and Pixel
motion photos are uploaded as a single file with an embedded video segment,
and Immich's own handling of that container was confirmed by reading Immich's
source — but whether Google Photos renders the result as a motion photo has
not been tested against a real account. If you try it, an issue report is
welcome either way.

## License

AGPL-3.0-only. See [LICENSE](LICENSE).
