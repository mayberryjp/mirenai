---
description: "Use when adding or extending a mirenai HTTP API endpoint (new route, CRUD, stats/list feed, or action). Scaffolds the schema -> repository -> route -> registration -> docs -> tests flow the mirenai way, with the right envelopes, validation helpers, and gate. Triggers: add endpoint, new route, new API, expose, GET/POST/PUT/DELETE."
name: "Add API endpoint"
argument-hint: "the endpoint to add — method, path, and behavior (e.g. 'GET /stats/foo — hourly foo counts per client')"
agent: agent
---

Add the API endpoint described in the argument. Follow mirenai's layering and conventions exactly
— read [AGENTS.md](../../AGENTS.md) first; this prompt is the per-endpoint checklist, not a
substitute for it. Do only what the endpoint needs; don't refactor unrelated code.

## Order of work

1. **Schema (boundary validation)** — [src/mirenai/api/schemas.py](../../src/mirenai/api/schemas.py).
   Only if the endpoint takes a request body. Add a `BaseModel` with `model_config =
   ConfigDict(extra="forbid")`; use `@field_validator` / `@model_validator` for constraints
   (reuse `_check_client`, `_check_port`, `_check_url`, `VALID_ACTIONS`, etc.). GET/list/action
   endpoints usually need no schema.

2. **Repository (all SQLAlchemy)** — `src/mirenai/repository/*.py`.
   Return **plain dicts/values, never ORM objects** (mirror an existing `_to_dict`). Use the right
   session scope for the target DB: `session_scope` routes each table to its own database
   (`config.db` / `stats.db` / `cache.db` / `querylogs.db`) via per-table binds;
   `blocklist_session_scope` (`blocklist.db`) and `hosts_session_scope` (`localhosts.db`) for those.
   For list feeds add both a
   `list_*(limit, offset, ...)` and a `count_*()`. Upserts use
   `sqlalchemy.dialects.sqlite.insert(...).on_conflict_do_update(index_elements=[...])`; in bulk
   upserts set `updated_at=func.datetime("now", "localtime")` explicitly (they bypass ORM
   `onupdate`). Datetimes → `.isoformat()` in the dict.

3. **Model / DB (only if new storage)** — [src/mirenai/repository/models.py](../../src/mirenai/repository/models.py)
   + [src/mirenai/db.py](../../src/mirenai/db.py). A **new table** on the correct base
   (`ConfigBase` / `StatsBase` / `CacheBase` / `QueryLogBase` / `BlocklistBase` / `HostsBase`) is
   auto-created by `init_db()` — nothing else. A **new column on an existing table** needs an
   idempotent `_ensure_*_column()` ALTER helper in `db.py` (mirror `_ensure_hosts_*`, targeting that
   table's `get_*_engine()`) called from `init_db()`.
   Timestamp columns default to `_LOCAL_NOW = text("(datetime('now', 'localtime'))")` — never
   `func.now()` / `CURRENT_TIMESTAMP`.
   Worker-local state (DNS cache, runtime gauges) is unreachable from the API process — relay it
   through the DB (write a row the worker polls, or read a row the worker flushed); do not try to
   share memory.

4. **Route** — `src/mirenai/api/routes/*.py` (new file → `register_<area>_routes(app)`).
   Success returns `{"status": "ok", <key>: ...}`; errors go through
   `error(code, message, status, detail=None)` from [src/mirenai/api/errors.py](../../src/mirenai/api/errors.py).
   Bodies: `model, err = parse_body(Model)` then read fields via **`model.model_dump()[...]`**
   (or `model.model_dump(exclude_unset=True)` for PUT) — attribute access fails mypy strict.
   Query params: `read_pagination()` for `limit`/`offset` (non-paginated → return all with
   `total=len(rows)`; paginated → `total=count_*()`), `read_int_query(name)` for optional ints.
   Path ints use the `<id:int>` filter; validate IPs with `ipaddress.ip_address` (bad → 422).

5. **Register** — add the `register_*_routes(app)` call in
   [src/mirenai/api/app.py](../../src/mirenai/api/app.py)::`create_app`. Order matters where paths
   overlap.

6. **Docs** — update [docs/frontend-integration.md](../../docs/frontend-integration.md) (envelope,
   error codes, per-endpoint JSON shape). Add a row to the API table in
   [README.md](../../README.md) only if this is a new endpoint/area. Do **not** paste TypeScript
   types into the docs.

7. **Tests** — add to the `test_*.py` matching the layer.
   Pure route tests can use the `client` `TestApp` fixture ([tests/conftest.py](../../tests/conftest.py)).
   DB-backed tests use a `temp_config_db` fixture (temp sqlite files + `db.create_all_schemas()`;
   an autouse fixture in conftest resets engines per test) — copy one from
   [tests/test_upstreams.py](../../tests/test_upstreams.py) or
   [tests/test_new_domains.py](../../tests/test_new_domains.py). Use
   `app.get/post(..., status=4xx)` for error cases (webtest raises otherwise).

## Finish

Run the full gate and make it pass clean:

```
python -m ruff check src tests
python -m mypy src
python -m pytest -q
python -m bandit -q -r src
```

Then summarize: files touched, the endpoint's envelope shape, and whether a fresh DB / DNS-worker
restart is needed to deploy it.
