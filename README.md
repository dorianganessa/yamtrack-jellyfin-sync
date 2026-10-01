# Yamtrack to Jellyfin additive sync

A small Docker service that reads Yamtrack's SQLite database and marks matching Jellyfin movies and episode files watched. It preserves existing watched states and uses Python's standard library, with no third-party Python dependencies.

Tested with Yamtrack 0.26.3 and Jellyfin 10.11.11. Movies match by TMDB ID; episodes match by the parent show's TMDB ID, season, and episode number. Rewatch records are deduplicated using their latest watched date. Every episode covered by a combined file must be watched before the file is marked played. Ambiguous editions and overlapping episode files are skipped and reported.

## Install with Docker Compose

Clone this repository, then create local configuration and runtime directories:

```sh
cp .env.example .env
mkdir -p state secrets
```

Edit `.env` with your Yamtrack database directory, Yamtrack user ID, Jellyfin user ID, and Jellyfin URL. The example Jellyfin ID is a placeholder. Use numeric UID and GID values that can read Yamtrack's database and write to the state directory.

Create a dedicated key in Jellyfin's Dashboard → API Keys. Save only the key value in `secrets/jellyfin_api_key` and set file permissions to `0600`. The file must be readable by the configured container UID; the state directory must be writable by that UID. Neither the secret directory nor `.env` is committed or included in the image build.

Mount the entire Yamtrack database directory, including SQLite WAL and SHM files. The bridge opens it with `mode=ro`, query-only mode, and a short read transaction. If read-only WAL access fails, the run fails; do not switch a live database to immutable mode or mount it writable. Use SQLite's backup API to produce a consistent snapshot if your filesystem cannot support read-only WAL access.

Validate, build, and inspect a dry run:

```sh
docker compose config -q
docker compose build
docker compose run --rm sync dry-run
```

The report is written to `state/latest.json`. Check candidate counts and skipped cases before enabling writes. A first small batch can be run explicitly:

```sh
docker compose run --rm sync sync --max-writes 5
```

After verifying the pilot, set `SYNC_DRY_RUN=false` in `.env` and start the scheduler:

```sh
docker compose up -d sync
docker compose logs --tail 5 sync
docker inspect --format '{{.State.Health.Status}}' yamtrack-jellyfin-sync
```

The default scheduler waits five minutes after each run and processes at most 100 proposed items per batch. It runs one batch at a time and uses a shared lock to prevent overlapping manual runs. Stop the scheduler before running manual commands:

```sh
docker compose stop sync
docker compose run --rm sync dry-run
docker compose run --rm sync sync --max-writes 500
docker compose up -d sync
```

The container runs without root, has a read-only root filesystem, publishes no ports, and does not mount Jellyfin's database or the Docker socket. For host-networked Jellyfin on Linux, the default `host.docker.internal:host-gateway` alias reaches the host. For Jellyfin on another machine or Docker network, set `JELLYFIN_URL` and attach an appropriate network.

## Finding user IDs

The Yamtrack user ID is the numeric `id` in its `users_user` table. Read it using SQLite's read-only mode, for example:

```sh
sqlite3 -readonly /path/to/yamtrack/db/db.sqlite3 'SELECT id, username FROM users_user;'
```

Jellyfin user IDs are returned by the authenticated `GET /Users` API. Retrieve the target user's ID using your preferred API client; keep the API key out of shell history and committed configuration. The bridge validates that the configured users exist before issuing writes.

## Trakt and webhook compatibility

If the target Jellyfin user has a Trakt integration, enable its option to skip unwatched imports. Otherwise an import can undo bridge changes. The bridge checks the plugin configuration each run and refuses writes when unwatched imports are enabled. It does not modify plugin preferences automatically.

If Yamtrack's manual watched-event processing is enabled, the bridge checks that active Jellyfin webhook plugins use the verified official plugin with playback-only destinations. Unknown plugins or manual user-data event configurations refuse writes to avoid feedback into Yamtrack. An ordinary Jellyfin → Yamtrack playback integration can remain enabled.

Dry runs report both compatibility checks without marking anything watched.

## Add to an existing homeserver stack

A main Compose file can build directly from a pinned public Git revision. Replace `OWNER` and `COMMIT_SHA`, and supply paths and user IDs through the stack's private environment file:

```yaml
services:
  yamtrack-jellyfin-sync:
    build:
      context: https://github.com/OWNER/yamtrack-jellyfin-sync.git#COMMIT_SHA
    restart: unless-stopped
    user: "${PUID:-1000}:${PGID:-1000}"
    read_only: true
    cap_drop: [ALL]
    security_opt: [no-new-privileges:true]
    environment:
      YAMTRACK_USER_ID: ${YAMTRACK_USER_ID:?Set YAMTRACK_USER_ID}
      JELLYFIN_USER_ID: ${JELLYFIN_USER_ID:?Set JELLYFIN_USER_ID}
      JELLYFIN_URL: ${JELLYFIN_URL:-http://host.docker.internal:8096}
      SYNC_DRY_RUN: ${SYNC_DRY_RUN:-true}
    extra_hosts:
      - host.docker.internal:host-gateway
    volumes:
      - ${YAMTRACK_DB_DIR:?Set YAMTRACK_DB_DIR}:/source:ro
      - ${SYNC_STATE_DIR:?Set SYNC_STATE_DIR}:/state
    secrets: [jellyfin_api_key]
    networks: [sync]
secrets:
  jellyfin_api_key:
    file: ${JELLYFIN_SECRET_FILE:?Set JELLYFIN_SECRET_FILE}
networks:
  sync: {}
```

Keep paths, user IDs, and key values in local configuration. Pin the Git revision so a deployment does not silently change application code. Run `docker compose build yamtrack-jellyfin-sync` when changing the pinned revision.

When moving an existing standalone installation into the main stack, stop its container first and keep the same state-directory and source mounts. Use different container names or remove the stopped old service before claiming its name. A main stack's `--remove-orphans` only applies to containers in that Compose project.

## Configuration and reports

`.env.example` lists Compose settings. `YAMTRACK_DB_DIR` is the source directory, `SYNC_STATE_DIR` holds reports, and `JELLYFIN_SECRET_FILE` points to the API key file. The source and state directories must already exist; Compose will not silently create them for a misspelled path.

Application settings include `YAMTRACK_DATABASE`, `YAMTRACK_USER_ID`, `JELLYFIN_USER_ID`, `JELLYFIN_URL`, `JELLYFIN_API_KEY_FILE`, `SYNC_STATE`, `SYNC_INTERVAL`, `SYNC_MAX_WRITES`, `SYNC_WRITE_DELAY`, `SYNC_HTTP_TIMEOUT`, `SYNC_HTTP_RETRIES`, `SYNC_REPORT_RETENTION`, and `SYNC_DRY_RUN`. Docker paths default to `/source/db.sqlite3`, `/state`, and `/run/secrets/jellyfin_api_key`.

`latest.json` contains the latest report; `run-*.json` retains the latest 100 reports. `in-progress.json` records confirmed changes during a batch, and `last-success.json` supplies the health check. Reports contain media titles and user activity, so keep the state directory private. Tokens and authentication headers are excluded from reports. Container logs rotate at three files of 5 MB each.

Already-watched items receive no writes. Missing library items are reconsidered each cycle so newly added media is discovered. A failed or uncertain POST is followed by a GET before a retry. The pre-write check reduces playback races but cannot make the API update atomic.

Stop future updates with `docker compose stop sync`. Previously applied watched flags remain. Ratings, notes, lists, resume positions, metadata repair, anime mapping, and unwatched changes are outside scope.

## Development

```sh
python3 -m unittest discover -s tests -v
docker compose --env-file .env.example config -q
```

Tests cover database user isolation, WAL reads, rewatches and subsecond dates, specials, combined files, duplicate matches, pagination, uncertain API responses, safety guards, and repeated-run idempotence. GitHub Actions runs the tests, validates Compose, and tests the built container.
