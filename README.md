# mirenai

A screening DNS resolver. It answers DNS queries differently depending on **who
is asking** and **what they are asking for**: forward to upstream resolvers,
return an override (sinkhole) answer, or deny with `NXDOMAIN`. A management API
(and separate frontend) configures the policy table, upstream resolvers, cache
behaviour, and exposes per-client query statistics.

## How screening works

Every decision comes from a single **policy** table. Each row screens one
`(client, domain)` pair:

| column              | meaning                                                        |
| ------------------- | -------------------------------------------------------------- |
| `client`            | source IP, or `*` for any client                               |
| `domain`            | exact name, `*.suffix` (name + subdomains), or `*` for any     |
| `action`            | `forward`, `override`, or `deny`                               |
| `override_response` | comma-separated IP(s) returned when `action = override`        |

Because both columns support wildcards, the three per-host modes fall out of one
table:

- **Allow everything** — `(host, *, forward)` forwards every lookup upstream.
- **Deny everything** — `(host, *, deny)` returns `NXDOMAIN` for every lookup.
- **Allow some** — one row per allowed domain (`forward` or `override`), and no
  wildcard row for that host. Anything not listed falls through to the default
  action, which is `deny` (`NXDOMAIN`).

`forward`, `override`, and `deny` can all be mixed per host and per domain, and
different clients can get different answers for the same name.

**Match precedence:** the most specific domain wins (exact > longest
`*.suffix` > `*`); ties are broken in favour of an exact client over `*`. When no
row matches at all, the configured `default_action` applies (default `deny`).

### Example

```text
client        domain              action     override_response
------------  ------------------  ---------  -----------------
10.0.0.5      *                   forward
10.0.0.9      *                   deny
10.0.0.20     github.com          forward
10.0.0.20     *.internal.example  override   10.0.0.53
# 10.0.0.20 asking for anything else -> NXDOMAIN (default deny)
```

## Components

- **DNS server** (`mirenai.workers.dns_server`) — serves UDP **and** TCP,
  applies policy, caches forwarded answers, and records query stats. Runs as its
  own process under supervisord.
- **API** (`mirenai.api`) — Bottle + Waitress. CRUD for policies, upstreams, and
  settings; read/reset for the query log; `/health` and `/ready`.

Policies, upstreams, and settings live in the database and are re-read by the DNS
server on a background timer (`refresh_seconds`), so changes made through the API
take effect without a restart.

## Caching

Forwarded responses are cached in memory (LRU, thread-safe) keyed by
`name|qtype|qclass`. The entry TTL is taken from the response records (SOA
minimum for negative answers), clamped to `[cache_min_ttl, cache_max_ttl]`.
Remaining TTL counts down on each cache hit, honouring the record's TTL. Caching
can be disabled at runtime (`cache_enabled`).

## Query statistics

The DNS server aggregates `(client, domain, qtype)` counts in memory and flushes
them to the `query_log` table on a timer (`query_flush_seconds`). Each flush
upserts: `count += n`, refreshing `last_seen`/`last_action` and preserving
`first_seen`. Read them at `GET /queries`; reset with `DELETE /queries`.

## API

| method   | path                | purpose                          |
| -------- | ------------------- | -------------------------------- |
| `GET`    | `/health`           | process alive                    |
| `GET`    | `/ready`            | database reachable               |
| `GET`    | `/policies`         | list policies (opt. pagination)  |
| `POST`   | `/policies`         | create policy                    |
| `GET`    | `/policies/{id}`    | fetch policy                     |
| `PUT`    | `/policies/{id}`    | update policy                    |
| `DELETE` | `/policies/{id}`    | delete policy                    |
| `GET`    | `/upstreams`        | list upstream resolvers          |
| `POST`   | `/upstreams`        | add upstream resolver            |
| `PUT`    | `/upstreams/{id}`   | update upstream resolver         |
| `DELETE` | `/upstreams/{id}`   | remove upstream resolver         |
| `GET`    | `/queries`          | per-client query statistics      |
| `DELETE` | `/queries`          | reset query statistics           |
| `GET`    | `/settings`         | effective runtime settings       |
| `PUT`    | `/settings`         | update runtime settings          |

Runtime settings (all in the database, editable via `PUT /settings`):
`cache_enabled`, `cache_max_ttl`, `cache_min_ttl`, `cache_max_entries`,
`forward_timeout`, `default_action`, `refresh_seconds`, `query_flush_seconds`,
`log_queries`.

## Configuration (environment)

Environment is kept to deployment concerns only; everything operational is in the
database. All variables are set in `docker-compose.yml`.

| variable              | default     | purpose                          |
| --------------------- | ----------- | -------------------------------- |
| `DATABASE_URL`        | `sqlite:////data/mirenai.db` | SQLite database DSN |
| `API_LISTEN_ADDRESS`  | `0.0.0.0`   | API bind address                 |
| `API_PORT`            | `8000`      | API port                         |
| `DNS_LISTEN_ADDRESS`  | `0.0.0.0`   | DNS bind address                 |
| `DNS_PORT`            | `53`        | DNS port (UDP + TCP)             |
| `LOG_LEVEL`           | `INFO`      | log level                        |
| `TZ`                  | `Asia/Tokyo` | container time zone       |

## Local runbook

```bash
make install          # pip install .[dev]
make initdb           # create the SQLite schema
make test             # pytest -q
make lint             # ruff check .
make typecheck        # mypy src
python -m mirenai.api_main            # run the API
python -m mirenai.workers.dns_server  # run the DNS server
```

Test the DNS server (defaults to port 53; use a high port locally if unprivileged):

```bash
dig @127.0.0.1 example.com A          # UDP
dig +tcp @127.0.0.1 example.com A     # TCP
```

## Docker

```bash
make docker-build     # docker build -t mirenai:dev .
make docker-run       # docker compose up
```

The container creates the SQLite schema, then runs the API and the DNS server
under supervisord. Because it runs as a non-root user, binding DNS port 53
requires the `NET_BIND_SERVICE` capability (already set in `docker-compose.yml`).
The SQLite database file is stored under `/data`, persisted via the
`mirenai-data` volume.

## Notes

- Override answers currently return `A`/`AAAA` records from the IP(s) in
  `override_response` (the common sinkhole case).
- Timestamps are stored and returned in the container's local time zone
  (`Asia/Tokyo`) via SQLite `datetime('now', 'localtime')`.
