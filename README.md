<div align="center">

# immich-gphotos

**Keep a second copy of your Immich library in Google Photos — automatically, free unlimited storage, original quality and without re-uploading what's already there.**

[![License: AGPL v3](https://img.shields.io/badge/License-AGPL%20v3-blue.svg)](LICENSE)
[![Python 3.12+](https://img.shields.io/badge/python-3.12+-blue.svg)](https://www.python.org/downloads/)
[![Immich 3.0+](https://img.shields.io/badge/immich-3.0+-4250af.svg)](https://immich.app)

</div>

> [!WARNING]
> **Read this before installing anything.** This project uploads through [gpmc](https://github.com/xob0t/gpmc), a **reverse-engineered** Google Photos mobile API. It is **unofficial**, it can break without notice when Google changes something, and using it carries **some risk to your Google account**. This project is **not affiliated with** Google or with Immich. Treat it as an extra copy of your photos — never as your only backup.

immich-gphotos is an open source service that mirrors your [Immich](https://immich.app) library into Google Photos and keeps it there. You run it yourself, next to Immich, in a single container.

It exists for one situation in particular: you've moved to Immich, you're happy there, and you'd still like your photos to live somewhere else too — the place your phone spent years uploading to, where sharing with family already works.

## Why you might want it

- 📸 **Every new photo, automatically.** Immich's own workflow system pushes new assets across the moment they land. No polling loop, no cron job, no export scripts.
- ⚡ **It won't re-upload your library.** Immich stores a SHA-1 checksum of every original, and that is exactly what Google Photos deduplicates on. Before moving a single byte it asks Google "do you already have this?" For most people — whose phone was uploading to Google long before they found Immich — the answer is usually yes, and the backfill costs almost no bandwidth.
- 🛟 **It notices what the webhook missed.** Immich's webhooks are fire-and-forget and never retried. A reconciler re-reads everything Immich changed since its last *complete* pass, so an asset created while the container was down still gets backed up. Nothing is ever known only by a webhook.
- 🖼️ **Motion photos stay motion photos.** Immich splits the video out of a motion photo into a separate hidden asset and leaves the original JPEG untouched. This uploads that original with its motion payload intact, and skips the extracted clip — so you don't end up with stray one-second videos in your timeline.
- 🗂️ **Albums come along.** Immich albums are recreated in Google Photos and kept in step, including splitting anything past Google's 20,000-item album limit.
- 🧯 **Deletions are opt-in and hard to trigger by accident.** Off by default, behind a typed confirmation, and guarded by a limit that refuses any pass which would trash more than 10% of your library. That's the switch you'll want if you ever restore Immich from an old backup.
- 🔑 **Credentials never go in your compose file.** You enter them once in a wizard that checks them against the real APIs before storing anything, and they're redacted everywhere they could otherwise surface — logs, events, error messages.

## Requirements

- **Immich 3.0 or newer** for the real-time path. Older versions still work — the wizard detects it and falls back to polling rather than refusing to set up.
- **Docker**, and a little disk for the container and its database.
- **An Android device** (or emulator) for a one-time credential extraction. This is the fiddly part, and it's unavoidable — but a [browser wizard](https://imaleeexx.github.io/immich-gphotos/) now does most of it over USB. It needs Chrome or Edge on a desktop; see [Getting your Google credential](#getting-your-google-credential).

## Quick start

**1. Create a `docker-compose.yml`:**

```yaml
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
      # Optional but recommended when Immich runs on this host. See
      # "Reading originals directly" below — the path must match the one
      # Immich's own container uses.
      - /path/to/immich/library:/usr/src/app/upload:ro
```

**2. Start it:**

```bash
docker compose up -d
```

No `chown` first. The container starts as root purely to take ownership of the
data directory — a bind-mounted `./data` arrives owned by whoever created it on
the host, and Docker creates it as root when it doesn't exist yet — then drops
to an unprivileged user before the app itself runs. Set `PUID`/`PGID` if you'd
rather those files belonged to some other account.

**3. Open `http://localhost:8080`** and set a password. The UI holds your Immich API key and your Google credential, so it isn't left open.

**4. Walk through the wizard.** Four screens, and each one proves the thing works before storing anything.

New photos start flowing immediately. The backfill works through your existing library in the background at a lower priority, so a photo taken today never queues behind eighteen thousand from 2014.

### The Immich API key

Create it in Immich under **Account Settings → API Keys**. It needs:

```
asset.read   asset.download   album.read   album.create   albumAsset.create
```

…plus these for real-time mode on Immich 3.0+:

```
workflow.create   workflow.read   workflow.update   workflow.delete   workflow.logs   plugin.read
```

The wizard names anything you've missed rather than just saying "insufficient permissions", so you can paste a key, see exactly what's wrong, and fix it in one pass.

Note there's no `asset.upload` in that list. This service never writes assets into Immich — the sync only goes one way.

## Getting your Google credential

There's no polite way around this one. Google offers no public API for uploading to your own library at original quality, so the credential has to come off an Android device. You only do it once.

### The browser wizard

**→ [Open the credential wizard](https://imaleeexx.github.io/immich-gphotos/)**

It talks to your phone over USB, installs the two apps it needs, watches the device log and hands you the finished `auth_data` string. The credential never leaves the browser tab — the page has no backend, and the only things it contacts are `api.github.com` to look up download links and a CDN for the USB library.

What you need:

- **Chrome, Edge, or another Chromium browser, on a desktop.** It uses WebUSB, which Firefox and Safari do not implement. A phone browser can't do this.
- **USB debugging** on the phone: Settings → About phone → tap *Build number* seven times, then Developer options → *USB debugging*. Use a cable that carries data.
- **No `adb` server running.** Android Studio, VS Code and scrcpy each start one, and it takes exclusive ownership of the phone — the wizard then can't reach it. Close them first. If connecting still fails, run `adb kill-server`; and if that appears to do nothing, try `sudo pkill -f "adb -L tcp:5037"`, because a root-owned server is invisible to `adb kill-server` and produces a USB transfer error that looks like a cable fault.

Two things worth knowing before you start. Play Protect **will** warn you when GmsCore installs — that app impersonates Google Play Services by design, which is precisely the pattern Play Protect flags; the wizard explains the dialog and how to get past it. And the patched Photos build installs under its own package name (`app.morphe.android.apps.photos`), so it sits alongside your normal Google Photos rather than replacing it. You'll end up with a second Photos icon.

The wizard is hosted on GitHub Pages rather than served by this container because WebUSB requires a secure context. `http://localhost:8080` qualifies, but `http://192.168.1.x:8080` does not — and that is how most people reach a self-hosted container.

### By hand

If you'd rather not use the wizard, or it won't connect:

1. Install [GmsCore](https://github.com/ReVanced/GmsCore/releases) and a [patched Google Photos APK](https://github.com/j-hc/revanced-magisk-module/releases). Note that repo publishes rolling releases — the newest one may not contain a Photos build, so look back a few.
2. Connect the device over ADB and run:

   ```bash
   adb logcat | grep "auth%2Fphotos.native"
   ```

3. Add your Google account **inside GmsCore**, then open Google Photos and pick it. The GmsCore sign-in is the one that emits the line.
4. Copy everything from `androidId=` to the end of the line. That's your `auth_data`.

The container's own setup wizard shows these steps inline with copyable commands, so you don't need to keep this page open while you work. If your device is rooted, the [gpmc README](https://github.com/xob0t/gpmc) documents an HTTP Toolkit method as well.

Treat that string like a password — it can't be rotated easily. This service redacts it from logs, events and error messages, but don't paste it into a GitHub issue. Because it can't be rotated, note that the wizard is a page served from GitHub Pages: the credential passes through JavaScript delivered by that host, which is a different trust posture from typing a command into your own terminal. The by-hand route above avoids it entirely.

## The setup wizard

| Step | What it does |
|---|---|
| **Immich** | Validates your URL and API key against the live server, reports the version, names any missing permissions, and tells you whether real-time mode is available. |
| **Google** | Validates `auth_data` with a lookup for a checksum no real file can have — proving the credential works without touching any of your media. |
| **Workflow** | Registers the workflow inside Immich for you, with a generated shared secret. Nothing to build by hand. |
| **Options** | Quality, albums, deletions, worker threads, bandwidth, and whether to start backfilling now. |

Settings changed later take effect immediately — no restart. That includes work already queued: changing or clearing the bandwidth cap releases any uploads that were deferred to wait out the old cap, so they are re-measured against the new one instead of serving out a sentence the cap they were charged under no longer justifies.

You run this wizard once per account — see [Multiple accounts](#multiple-accounts) below.

## Multiple accounts

One account is one Immich library mirrored to one Google Photos library: its own Immich API key, its own Google `auth_data`, its own queue, and its own background loop. A household backing up three people's Immich users to three separate Google accounts runs three accounts in this one container, all managed from the single admin login you set on first boot.

Add one from **Accounts** in the nav: give it a label and you're dropped straight into the setup wizard for it. That wizard needs a fresh Immich API key and a fresh `auth_data` for this account — not the ones you used for another one. Both are inherently per-person: Immich API keys are minted by whoever is logged into Immich as that user, so an admin cannot create one on someone else's behalf, and `auth_data` comes off whichever Google identity the browser credential wizard (or the by-hand steps) was run against. There's no shortcut around running each step once per account.

Every account gets its own webhook URL, `http://<host>:8080/hooks/immich/<account id>`. The wizard's Workflow step fills in the right one automatically for whichever account you're currently setting up, so you never have to look up or copy an id by hand.

The switcher in the nav picks which account you're looking at; Settings, Failures and Diagnostics all follow it. Quality, album sync and deletions are per account — each library keeps its own choice. Worker threads and the bandwidth cap are global instead: one uplink and one machine are shared by every account on the install, so changing either from any account's Settings page changes it for all of them.

To remove an account, use **Remove…** on the Accounts page and type its label back to confirm. This stops its backups and forgets its queue; it never deletes anything from Google Photos. It also tries to delete the account's workflow inside Immich, but that part is best effort — if Immich is unreachable or the API key is already dead, the removal still completes and the page tells you to delete the workflow by hand instead (left alone, it keeps posting into a webhook nothing answers anymore). A separate checkbox lets you also delete the account's data on disk; leave it unchecked to keep the database around.

## Upgrading from a single-account install

Upgrading the container image is enough on its own — there's no migration command to run. The first boot after the upgrade adopts your existing database as an account named **Default**, keeping your admin password, your current login session, and your configured bandwidth cap exactly as they were. The workflow already registered in Immich keeps posting to the same bare `/hooks/immich` URL it always has, so there's nothing to change on the Immich side either.

That bare URL stays bound to the account the migration adopted for as long as the container runs — it doesn't move if you later remove that account or add others. Any account you add after upgrading gets its own `/hooks/immich/<account id>` URL, same as on a fresh install.

## Security

Every account's Immich API key and Google `auth_data` live in the same `./data` volume, behind the one admin password you set on first boot. There is no wall between accounts inside that volume: anyone who can read the volume's files, or who knows the admin password, can act as every account configured on this install, not just one.

That matters most for `auth_data`. It isn't a scoped, revocable "upload to this app" token — it's the credential an Android device uses to authenticate to Google Photos as a full account, the same access the Google Photos app on your phone has. Whoever holds it can read, upload to, or trash that Google account's library, in full.

Per-account encryption at rest isn't offered, and it isn't a gap the project intends to close later: this service exists to keep syncing in the background while nobody is logged in, so the process has to be able to decrypt every stored credential on its own, without a person unlocking anything first. A vault that only opens for a logged-in admin would defeat that. So the real boundary here is the host, not the app: keep `./data` readable only by the account this container runs as, protect any backup of it as carefully as you'd protect the credentials themselves, and treat the admin password as what it actually is — the single key to every Google and Immich account behind it.

## Settings

| Setting | What it does |
|---|---|
| **Quality** | Original quality without touching your storage quota (the default), Storage Saver, or counted against quota. |
| **Albums** | Mirror Immich albums into Google Photos. |
| **Deletions** | Propagate deletions to Google's trash. Off by default — see below. |
| **Worker threads** | How many uploads run concurrently. Shared by every account — see [Multiple accounts](#multiple-accounts). |
| **Bandwidth cap** | Upload throughput limit for metered or shared connections. Leave blank for no limit. Shapes long-run average throughput, not the instantaneous rate — see below. Shared by every account — see [Multiple accounts](#multiple-accounts). |

Schedule window, content filters (size caps, RAW, tags, album allowlist) and retry behaviour exist in the engine — the engine correctly gates transfer on a configured window and applies sensible defaults for the rest — but are not yet exposed for editing in this release.

**What the bandwidth cap actually limits.** Each upload waits out its share of the configured rate before that file's bytes move, queueing behind whatever was charged against the cap ahead of it, then the transfer itself runs unthrottled — so the cap holds the average bytes-per-second across uploads to the configured rate, but does not meter the instantaneous rate during any single transfer, which can briefly saturate the link while it runs. A wait long enough to freeze the background loop for an appreciable time (a low cap, a large file, or simply a backlog queued behind both) is not slept out inline: the upload is deferred and retried once that wait has elapsed, so the loop's other work — reconcile, backfill, album sync, the deletion sweep — is never blocked on it.

## Deletions

Off by default, and opt-in through an explicit toggle with a typed confirmation. When enabled, the reconciler detects assets trashed in Immich and trashes the corresponding item in Google Photos — using Google's own trash and its 60-day recovery window, never a permanent delete.

**Safety limit.** If a single pass would trash more than 10% of the synced library, or more than 500 assets, whichever comes first, the service refuses that pass and raises an alert instead. This guards against an Immich database restored from an old backup being read as "everything was deleted." Neither threshold can be disabled.

**Known limitation.** Detection relies on Immich still reporting the asset as trashed at the next reconcile. An asset removed so completely that it never passes through that state while this service is watching — a hard delete while the container is stopped, for instance — is not currently detected or propagated. Closing that gap needs a separate full-library comparison pass, which does not exist yet.

## Reading originals directly

`originalPath` arrives in every webhook payload and reconcile page. If the container can read that path itself, the byte resolver hands it straight to the uploader: no copy, no scratch space, no round trip through the Immich API to download the file first.

This only works if the path *inside this container* is the same path Immich's own container sees — which is why the compose snippet mounts the library read-only at `/usr/src/app/upload`, Immich's default upload location. Match it to wherever your Immich container actually mounts its library.

Without the mount, or when a file isn't readable, it falls back automatically to downloading through the Immich API into a scratch directory, uploading, then cleaning up. Nothing breaks without it — this is a performance path, not a requirement. The dashboard shows which mode is active.

Set `IGP_ALLOW_DIRECT_READS=false` to force the API-download path even when the mount is present.

## Environment variables

All optional, all with working defaults, and **none of them ever carry a credential** — those live only in the database, entered through the wizard.

| Variable | Default | Purpose |
|---|---|---|
| `IGP_DATA_DIR` | `/data` | Database and scratch location. |
| `IGP_LOG_LEVEL` | `INFO` | Log verbosity. |
| `IGP_HOST` / `IGP_PORT` | `0.0.0.0` / `8080` | Listen address. |
| `IGP_SCRATCH_DIR` | `<data>/scratch` | Where downloaded originals are staged. |
| `IGP_ALLOW_DIRECT_READS` | `true` | Set `false` to always download through the Immich API. |
| `PUID` / `PGID` | `1000` / `1000` | Which user owns the data directory and runs the app. |

## Monitoring

- `GET /healthz` — liveness, no auth required.
- `GET /metrics` — Prometheus counters for assets by state and whether transfer is paused, no auth required. Every series carries an `account` label, so a multi-account container reports each account separately:

  ```
  immich_gphotos_assets_total{account="9f3c1a…",state="synced"} 1284
  immich_gphotos_assets_total{account="9f3c1a…",state="failed"} 3
  immich_gphotos_paused{account="9f3c1a…"} 0
  immich_gphotos_paused{account="b7d204…"} 1
  ```

  The label is the account's opaque id, never the name you gave it: `/metrics` is unauthenticated by design, so nothing user-supplied goes in it. Alert on `immich_gphotos_paused` per account — a paused account is one whose credentials need re-entering, and nothing else about the container will look wrong. A container with no accounts configured yet still answers 200, with no series.
- Structured JSON logs on stderr, with credentials redacted by exact match *and* by pattern.
- A diagnostics page that also surfaces Immich's own workflow logs, so when the webhook path misfires you can read Immich's account of it rather than guessing.

## How it works

```
Immich ──workflow(AssetCreate)──> webhook ──> queue
                                               │
Immich REST <──── reconciler (every 15m) ──────┤
                                               ▼
                                     ask Google: got this hash?
                                       │                  │
                                    yes│                  │no
                                       ▼                  ▼
                                 mark synced        read bytes ──> upload
                                                 (direct mount | API download)
```

The asset table *is* the work queue, so there's no second store to drift out of sync with it. Workers claim rows atomically, and every step is idempotent — a crash anywhere is recovered by simply running that asset again.

## What has actually been verified

Tested end to end against a real Immich 3.2.2 instance and a real Google Photos account:

- ✅ The `AssetCreate` webhook fires and its payload parses, including the `Buffer`-shaped checksum field.
- ✅ That checksum is the SHA-1 of the original file — the assumption the entire dedup design rests on.
- ✅ A hash Google already holds is recognised without uploading anything.
- ✅ Upload, dedup lookup and trash all work through the real API.
- ✅ Motion photos: with real Pixel and Samsung samples, Immich extracts the video into a hidden asset, leaves the original JPEG byte-identical, and the eligibility rule uploads the still while skipping the clip.

**Not verified:** whether Google renders those uploaded motion photos as motion photos. The bytes go up intact, which is what matters mechanically, but nobody has confirmed the result in a real account — so this says so rather than implying more.

**Not verified:** the browser credential wizard, past the point of connecting to a device. The USB connection, device authorisation and installed-app detection are exercised; the streamed APK install, the app launch and the log capture that extracts the credential are written against the ADB library's published interfaces but have not yet been run end to end against real hardware. If it fails for you, the by-hand steps above are the fallback, and a bug report with the wizard's log panel contents is genuinely useful.

## Contributing

The engine tests entirely against fakes. No Google account, no network, no credentials:

```bash
uv venv --python 3.12 && uv pip install -e ".[dev]"
pytest
```

Both external surfaces sit behind Protocols with in-memory fakes, so you can work on the sync engine, the queue or the UI without any credentials at all. A separate contract-test suite runs against Immich's published OpenAPI spec on a schedule, so an Immich release that moves something we depend on fails a build rather than someone's backup.

## License

AGPL-3.0. See [LICENSE](LICENSE).

Built on [gpmc](https://github.com/xob0t/gpmc) by xob0t, which does the genuinely hard part.
