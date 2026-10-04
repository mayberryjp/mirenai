# mirenai — agent guide

mirenai is a per-client screening DNS resolver (SQLite-backed, single container) with a
Bottle HTTP management API. Read [README.md](README.md) first for the product behavior and the
policy/screening model; this file covers what isn't obvious from a first read.

## Setup & checks

- Python 3.12+. Install dev deps: `make install` (`pip install .[dev]`).
- Full gate — all four must pass clean before you call a change done:
  ```
  python -m ruff check src tests
  python -m mypy src
  python -m pytest -q
  python -m bandit -q -r src
  ```
- `make lint` / `make typecheck` / `make test` cover the first three. **bandit has no Makefile
  target** — run it explicitly (it is a dev dependency).
- Create the SQLite schema for a local run: `make initdb`.

## Architecture

Three long-running processes plus a one-shot init, all under supervisord in one container
(see [supervisord.conf](supervisord.conf)):

- `mirenai.api_main` — Bottle/Waitress HTTP API.
- `mirenai.workers.dns_server` — UDP+TCP DNS server; applies policy, caches, records stats.
- `mirenai.workers.blocklist_downloader` — refreshes blocklists on a schedule.
- `mirenai.workers.local_zones` — refreshes local DNS zones (Git-hosted `ip,domain` files,
  served authoritatively from the DNS worker's memory) on a schedule.
- `initdb` — `mirenai.db.init_db()` runs once at startup.

Keep changes in the right layer:

- `domain/` — pure logic (resolver, policy, cache, in-memory buffers). No Bottle, no request parsing.
- `repository/` — all SQLAlchemy; returns plain dicts/values across the boundary, not ORM objects.
- `api/routes/` + `api/schemas.py` — HTTP surface; Pydantic validates **at the boundary only**.
- `workers/` — process entrypoints and the daemon threads that flush buffers to the DB.

## Conventions that bite

- **Three SQLite databases, no migration tool.** Config `mirenai.db` (`Base`), blocklist domains
  `blocklist.db` (`BlocklistBase`), known clients `localhosts.db` (`HostsBase`). `init_db()`
  `create_all`s all three. A **new table** is auto-created — nothing else to do. A **new column on
  an existing table** needs an idempotent `_ensure_*_column()` ALTER helper in
  [src/mirenai/db.py](src/mirenai/db.py) (mirror the existing ones), because `create_all` never
  alters existing tables.
- **DB-relay for DNS-worker state.** The DNS cache and runtime gauges live in the DNS worker
  *process*; the API is a *separate* process and cannot read or mutate them in memory. Cross-process
  actions go through the DB — e.g. `POST /cache/flush` writes a timestamp the worker polls, and
  `GET /stats/runtime` reads gauges the worker flushes. Never assume shared memory between the API
  and the workers.
- **Local-time timestamps.** Timestamp columns default to
  `_LOCAL_NOW = text("(datetime('now', 'localtime'))")` so stored times follow the container `TZ`.
  Do **not** use `func.now()` / `CURRENT_TIMESTAMP` (they store UTC). Bulk inserts bypass ORM
  `onupdate`, so set `updated_at=func.datetime("now", "localtime")` explicitly in those upserts.
- **Resolver / RuntimeState constructors are breaking surfaces.** `DnsResolver.__init__` and
  `RuntimeState` take a growing list of collaborators (stats / requests / rtt buffers; state
  loaders). When you add one, update **both** `workers/dns_server.py` (state build + main wiring)
  **and** `tests/test_resolver.py::_make_resolver`, or the suite breaks.
- **Uniform API envelopes.** Success returns `{"status": "ok", <key>: ...}`; errors go through
  `api/errors.py::error(code, message, status)`. Reuse `read_pagination()` / `read_int_query()` for
  query params. Register every new route module in `api/app.py::create_app` (order matters where
  paths overlap). `parse_body(Model)` returns a generic `BaseModel` — read fields via
  `model.model_dump()[...]`, not attribute access (mypy strict flags the latter).
- **Upserts** use `sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_update(index_elements=[...])`.

## Testing

- [tests/conftest.py](tests/conftest.py) sets `DATABASE_URL=sqlite://` and exposes a `client`
  webtest `TestApp` fixture for route tests.
- DB-backed tests use a `temp_config_db` fixture (temp sqlite file, null out `db._engine` /
  `db._session_factory`, `create_all`) — copy an existing one, e.g. [tests/test_new_domains.py](tests/test_new_domains.py).
- Put tests next to the layer you touch: resolver behavior in `tests/test_resolver.py`, repos and
  routes in their matching `test_*.py`.

## Tooling gotchas

- ruff lint select is only `E4/E7/E9/F` — no `S` rules (bandit covers security separately).
- mypy is `strict` with the pydantic plugin (already configured); `db`, `api.app`, and
  `api.routes.*` allow untyped decorators.
- bandit `# nosec Bxxx` must be **bare** on the flagged line — trailing prose is parsed as extra
  test IDs and emits warnings; put the reason on a separate comment line above. `urlopen` needs an
  inline `# nosec B310`.
- Outbound HTTP `User-Agent` must contain `/u/homelabids`.

## Keep in sync

- API changes → update [docs/frontend-integration.md](docs/frontend-integration.md) (envelopes,
  error codes, per-endpoint JSON shapes).
- Behavior or config changes → update [README.md](README.md).
- Env vars are deployment-only and declared in [docker-compose.yml](docker-compose.yml); all
  operational config lives in the DB and is edited through the API.
