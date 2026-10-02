# Mahalla Sync

A Django application that synchronizes Uzbekistan's mahalla classifier every
minute, with a browser directory, REST API, filters, and synchronization history.
Both environments run the web app, scheduler, and PostgreSQL through Docker Compose.

## Two standalone environments

Install Docker with Docker Compose (Docker Desktop includes both). Each file is
complete on its own. Use exactly one `-f` file per command; the development file
is not an override of the production file.

| | Production | Development |
| --- | --- | --- |
| Compose file | `docker-compose.yml` | `docker-compose.dev.yml` |
| Project / network prefix | `mahalla-sync` | `mahalla-sync-dev` |
| Default URL | http://localhost:8000/ | http://localhost:8001/ |
| Web server | Gunicorn, two workers | Django runserver with code reload |
| Debug | Off | On |
| Source files | Copied into the image | Bind-mounted from this directory |
| Dependencies | Runtime dependencies | Runtime dependencies plus Ruff |
| Static files | Collected and served by WhiteNoise | Served directly from source |
| PostgreSQL volume | `mahalla-sync_postgres_data` | `mahalla-sync-dev_postgres_data` |
| Default database | `mahalla_sync` | `mahalla_sync_dev` |

The two environments have separate PostgreSQL services, volumes, networks,
credentials, and images. They can run simultaneously. The production project
name and volume preserve data from the original Docker setup.

## Production

On a fresh checkout, copy `.env.example` to `.env` and fill in
`POSTGRES_PASSWORD` and `DJANGO_SECRET_KEY` with unique values. Both are required;
there are no default production secrets. An existing `.env` should be retained.
Set `DJANGO_ALLOWED_HOSTS` to the hostname you will use. Then run:

```sh
docker compose -f docker-compose.yml up --build -d
```

The current workspace already has a private `.env` configured for its database.
It is ignored by version control and excluded from Docker builds. Production
always sets `DJANGO_DEBUG=0`. Code changes require rebuilding the image.
Gunicorn allows 180 seconds for a manual synchronization. Both app services run
as a non-root user and restart after unexpected exits.

The supplied port binding is loopback, suitable for a reverse proxy. For public
access, configure HTTPS at that proxy and add authentication/rate limits to the
manual sync endpoint. The app connects directly to PostgreSQL; a transaction-pool
proxy is incompatible with its session-level synchronization lock.

## Development

No `.env` or production credentials are required:

```sh
docker compose -f docker-compose.dev.yml up --build -d
```

Open **http://localhost:8001/**. Development only reads optional `DEV_*`
configuration values, so production settings in `.env` cannot redirect it to
the production database. Change `DEV_WEB_PORT` to use a different local port.

Source changes are immediately visible inside both containers. Django reloads
the web process after Python changes; CSS and templates are served from source.
Restart the scheduler after changing synchronization code:

```sh
docker compose -f docker-compose.dev.yml restart scheduler
```

Dependencies live at `/opt/venv` in the image, outside the source mount. Rebuild
after changing `pyproject.toml` or `uv.lock`. No local Python or virtual
environment is required.

## Everyday commands

Choose the relevant file for each command. Development examples:

```sh
docker compose -f docker-compose.dev.yml ps
docker compose -f docker-compose.dev.yml logs -f web scheduler
docker compose -f docker-compose.dev.yml exec web python manage.py sync_mahallas
docker compose -f docker-compose.dev.yml exec web python manage.py createsuperuser
docker compose -f docker-compose.dev.yml down
```

Use `-f docker-compose.yml` for the equivalent production operation. Each
scheduler syncs immediately on startup, then every 60 seconds. PostgreSQL must
be healthy before migrations run, and the scheduler starts after the web
service becomes healthy. The first download usually takes 10–45 seconds.

Data survives restarts, image rebuilds, and `down`. Removing an environment's
named volume (for example, with `down -v`) deletes that environment's database.
Changing a password in `.env` does not change a role in an existing PostgreSQL
volume; update the database role as well when rotating its password.

To back up production:

```sh
docker compose -f docker-compose.yml exec -T db sh -c 'pg_dump -U "$POSTGRES_USER" "$POSTGRES_DB"' > backup.sql
```

## How the external data was discovered

The supplied [classifier page](https://cs.egov.uz/#/tableData/65d3205dcb6031b39c078af7)
is a Vue application. Inspecting its public JavaScript bundle's `tableData` route
revealed two requests:

- `GET https://cs.egov.uz/apiClient/Main/GetById?id=65d3205dcb6031b39c078af7`
  returns the classifier's schema and metadata.
- `POST https://cs.egov.uz/apiData/MainData/Get` returns its paginated records.

The actual data request is:

```json
{"id":"65d3205dcb6031b39c078af7","offset":0,"limit":1000,"query":{}}
```

The response wraps `result.count`, `result.data`, and `result.tableStruct` in an
envelope containing `statusCode` and `error`. Each record has `dateTime` and
`tableColumn`, whose cells expose a `data` value. This application calls that
service directly; it does not scrape rendered HTML.

Live inspection on 2 October 2026 returned 8,992 records. All TINs were present
and unique, whereas two records lacked both `code` and `uzcad_code`. Therefore
`tableColumn.tin.data` (TIN / STIR) is the unique `external_id`. The source's
internal row ID is not used as identity, since it may change when data is
republished. A future missing or duplicate TIN rejects the snapshot rather than
silently changing identity.

| Local field | Source |
| --- | --- |
| `external_id` | `tableColumn.tin.data` |
| `name` | `tableColumn.name_uz_latin.data` |
| `region` | `tableColumn.region_soato.data` |
| `district` | `tableColumn.district_soato.data` |
| `source_updated_at` | `dateTime`, nullable if absent |
| `is_active` | Presence in the most recent complete snapshot |

Region and district are **SOATO codes**, because the service does not include
their display names. They are labeled accordingly in the UI. Names are trimmed,
whitespace collapsed, and Unicode normalized. Local timestamps are stored with
timezone support and shown in Asia/Tashkent.

One observed API quirk: `result.isSuccess` is `false` even for valid results.
The official frontend ignores it. This client instead checks the HTTP status,
outer `statusCode`, `error`, classifier metadata, counts, and record structure.

## Synchronization and data safety

`mahallas/source.py` fetches pages of 1,000 with a 5-second connection timeout,
a 20-second read timeout, and a 120-second overall budget checked between
requests. The next scheduled run serves as a retry; there is no tight retry loop.

Before any mahalla write, it validates every page, identifier, required field,
timestamp, and total count. It rejects duplicate TINs, prematurely empty pages,
unexpectedly empty datasets, wrong classifiers, and a count or `lastUpdate`
revision that changes between pages. A 100,000-record ceiling bounds accidental
runaway pagination.

`mahallas/sync.py` then loads existing records once, compares by TIN, and uses
batched `bulk_create` / `bulk_update` inside one transaction. Identical records
are not written, so their local `updated_at` stays unchanged. A unique database
constraint provides a second line of defense against duplicates. The database
and successful log commit together. Any write failure rolls back the entire
snapshot; a failed log is saved separately.

Records missing from a fully validated, nonempty snapshot are marked inactive,
never deleted. They reactivate if they return. This policy also retains old
records for inspection. Both the API and directory include inactive records;
the page reports total and active counts separately. `updated` includes changes
to active status. `unchanged` counts unchanged records present in the snapshot.
On failure, `failed: 1` means one failed synchronization, not one rejected row;
partial imports are intentionally unsupported.

## Scheduling and concurrent runs

The scheduler's `--watch` mode runs immediately, then every 60 seconds measured
from the start of a run. Failed syncs are logged and the loop continues. If a
run exceeds a minute, missed ticks are skipped. Production Compose restarts either app
service after an unexpected process exit. No broker or task queue is needed.

The manual endpoint, one-shot command, and scheduler use the same nonblocking
[PostgreSQL advisory lock](https://www.postgresql.org/docs/17/explicit-locking.html#ADVISORY-LOCKS).
The database session holds it across fetching and committing, so it coordinates
separate containers and web workers. The fetch does not hold an open database
transaction; only the writes and success log are transactional.

A competing manual request returns HTTP 409 and a scheduler attempt skips the
run. The lock is explicitly released in `finally`; PostgreSQL also releases it
when its owning session disconnects. The next owner marks any interrupted
running log as failed. All application instances must point at the same database.

## HTTP interface

- `GET /` — directory, filters, pagination, sync button, latest status, and last
  five runs. Manual sync refreshes the page; refresh to see automatic runs.
- `GET /api/mahallas/?page=1&page_size=25&search=nav&region=1726&district=1726266`
  — name substring search, exact SOATO filters, stable name/TIN ordering. Page
  size is 1–100. Returns `count`, `page`, `page_size`, `total_pages`, `next`,
  `previous`, and `results`. Bad parameters return 400; missing pages return 404.
- `POST /api/mahallas/sync/` — synchronously fetches and applies the complete
  snapshot, returning `created`, `updated`, `unchanged`, `failed`. Returns 409
  while another run owns the lock and 502 if synchronization fails.
- `GET /api/mahallas/sync/history/` — latest 20 runs, newest first, including
  timestamps, counts, duration, status, and error message.

The sync endpoint retains Django's CSRF protection. The browser button handles
the token. To call it from the command line:

```sh
curl -c /tmp/mahalla-cookies http://localhost:8000/ -o /dev/null
csrf_token=$(awk '$6 == "csrftoken" {print $7}' /tmp/mahalla-cookies)
curl -b /tmp/mahalla-cookies -H "X-CSRFToken: $csrf_token" \
  -X POST http://localhost:8000/api/mahallas/sync/
```

## Tests and checks

Run tests in development against PostgreSQL. Django creates and removes a
separate test database; the development and production data are unaffected.
Ruff is already installed in the development image.

```sh
docker compose -f docker-compose.dev.yml exec web python manage.py test --noinput
docker compose -f docker-compose.dev.yml exec web python manage.py check
docker compose -f docker-compose.dev.yml exec web python manage.py makemigrations --check --dry-run
docker compose -f docker-compose.dev.yml exec web ruff check .
docker compose -f docker-compose.dev.yml exec web ruff format --check .
```

Tests mock the external service and cover create/update/idempotency, missing
and returning records, source and database failures, interrupted runs,
PostgreSQL locking between separate sessions/processes, pagination, CSRF,
APIs, the page, scheduler cadence, and a 10,001-record import with bounded queries.

## Improvements with more time

- Join an official SOATO directory to show region/district names.
- Add a job queue if manual sync needs to outlast the HTTP timeout budget.
- Add monitoring, scheduler health, log retention, and authenticated manual sync.
- Verify an upstream snapshot/version mechanism. Revision checks cannot ensure
  consistency if the source changes data without changing its advertised revision.
# mahalla-sync
