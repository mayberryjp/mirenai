# mirenai

**Per-client DNS filtering for your network.** mirenai decides what to do with
every DNS query based on *who is asking* and *what they are asking for*: forward
it to an upstream resolver, answer with a sinkhole address of your choosing, or
refuse it with `NXDOMAIN`. Point your devices at it and every lookup runs through
rules you control, managed over a small HTTP API and a separate web UI.

It sits in the same family as Pi-hole and AdGuard Home, with one difference that
drives the whole design: decisions are **per client**. The kids' tablet, the IoT
VLAN, and your own laptop can each get a different answer for the same name, all
from one policy table.

> Self-hosted homelab project, currently `v0.1.0`. One container, SQLite for
> storage, plain DNS over UDP and TCP. No external database or extra services to
> run.

## What it does

- **Per-client policies** — allow, deny, sinkhole, or blocklist a name, scoped to
  a single client IP or applied to everyone with `*`.
- **Different answers per client** — the same domain can forward for one host and
  return `NXDOMAIN` for another.
- **Sinkholes and overrides** — hand back your own A/AAAA records for a name: kill
  ad domains, pin an internal service, do split-horizon DNS.
- **Global blocklists** — feed it hosts-format, domain-list, or Adblock/uBO
  (`||domain^`) URLs; it downloads and refreshes them on a schedule and blocks
  listed names (and their subdomains) for every client. Exempt any device that
  needs the real answer.
- **IPv4-only mode** — one toggle makes every AAAA query come back empty (NODATA),
  so clients fall back to A instead of hanging.
- **Trusted-network screening** — list the source subnets (CIDR) you serve and
  every query from outside them is silently dropped over both UDP and TCP, with a
  "foreign networks" counter in the per-client and site-wide graphs. Configure no
  subnets and it answers everyone, as before.
- **Caching that stays out of the way** — in-memory, LRU, TTL-aware, thread-safe.
  Turn it off at runtime or flush it from the API.
- **Live configuration** — policies, upstreams, blocklists, and settings live in
  the database and are re-read on a timer. Change a rule and it takes effect in
  seconds, no restart.
- **Real visibility** — per-client query counts, a searchable query log, a live
  per-client request/response tail, a feed of newly-seen domains, and runtime
  gauges like cache size and blocklist size.
- **Device names, optionally** — sync friendly names and icons for each client
  from [Sando](https://github.com/mayberryjp/sando).
- **One moving part** — the DNS server, blocklist downloader, and API run together
  under supervisord, backed by SQLite on a single volume.

## How screening works

Every decision comes from one **policy** table. Each row screens a single
`(client, domain)` pair:

| column              | meaning                                                        |
| ------------------- | -------------------------------------------------------------- |
| `client`            | source IP, or `*` for any client                               |
| `domain`            | exact name, `*.suffix` (name + subdomains), or `*` for any     |
| `action`            | `forward`, `override`, `deny`, or `blocklist`                  |
| `override_response` | comma-separated IP(s) returned when `action = override`        |

**Match precedence:** the most specific domain wins (exact > longest `*.suffix` >
`*`); ties go to an exact client over `*`. If nothing matches, the site-wide
`default_action` applies (ships as `forward`).

Because both columns take wildcards, the common setups are just rows:

- **Allow a client** — `(host, *, forward)` forwards every lookup upstream.
- **Block a client** — `(host, *, deny)` returns `NXDOMAIN` for every lookup.
- **Whitelist a client** — a `(host, *, deny)` wildcard plus one `forward` or
  `override` row per allowed name.
- **Sinkhole a name** — `(*, ads.example.com, override, 0.0.0.0)` for everyone, or
  scope the same row to a single client.

For the plain allow-all / block-all case there is a shortcut: `PUT
/clients/{ip}/mode` with `forward` or `deny` flips a client's wildcard row without
touching its per-domain exceptions.

**Blocklists are global.** Enabled blocklists apply to *every* client's queries by
default, so you don't need a `blocklist` policy row to get filtering. To exempt a
device, set `excluded_from_blocklist` on its host. To exempt a single *domain* from
every blocklist, add it as an override (`POST /blocklists/overrides`); override domains
are stripped from each list when it is downloaded, so the exemption applies on the
list's next refresh rather than at query time.

**New-domain monitoring is per-client.** A client's newly-seen domains show up in
`GET /stats/new-domains/recent` by default; clear `flag_new_domains` on its host to
hide that client's new domains from the feed.

**New clients** are picked up automatically. The first time an IP sends a query it
gets an explicit `(client, *, <default_action>)` row mirroring the current site
default, and — if `SANDO_API_URL` is set — its name and icon are synced from
Sando (also on demand via `POST /hosts/{id}/sync`).

### Example

```text
client        domain              action     override_response
------------  ------------------  ---------  -----------------
10.0.0.5      *                   forward
10.0.0.9      *                   deny
10.0.0.20     github.com          forward
10.0.0.20     *.internal.example  override   10.0.0.53
# 10.0.0.20 asking for anything else -> NXDOMAIN (its wildcard is deny)
```

## Quickstart

```bash
make docker-build     # docker build -t mirenai:dev .
make docker-run       # docker compose up
```

The container builds the SQLite schema, seeds a single upstream (`8.8.8.8`) and a
default (disabled) blocklist, and starts the API and DNS server. Then point a
device — or your whole network, from the router — at the host's IP for DNS:

```bash
dig @<host-ip> example.com A          # UDP
dig +tcp @<host-ip> example.com A     # TCP
```

A fresh install ships with `default_action = forward`, so it resolves for everyone
out of the box. Lock it down by adding policies or switching `default_action` to
`deny`.

> Binding DNS on port 53 as a non-root user needs the `NET_BIND_SERVICE`
> capability, which `docker-compose.yml` already grants.

## Configuration

Environment variables cover deployment only; everything operational lives in the
database and is edited through the API. All of these are set in
`docker-compose.yml`:

| variable                  | default                         | purpose                                   |
| ------------------------- | ------------------------------- | ----------------------------------------- |
| `DATABASE_URL`            | `sqlite:////data/mirenai.db`    | configuration database                    |
| `BLOCKLIST_DATABASE_URL`  | `sqlite:////data/blocklist.db`  | blocklist-domains database                |
| `LOCALHOSTS_DATABASE_URL` | `sqlite:////data/localhosts.db` | known-clients database                    |
| `API_LISTEN_ADDRESS`      | `0.0.0.0`                       | API bind address                          |
| `API_PORT`                | `8000`                          | API port                                  |
| `DNS_LISTEN_ADDRESS`      | `0.0.0.0`                       | DNS bind address                          |
| `DNS_PORT`                | `53`                            | DNS port (UDP + TCP)                      |
| `LOG_LEVEL`               | `INFO`                          | log level                                 |
| `SANDO_API_URL`           | *(empty)*                       | Sando base URL for name/icon sync; empty disables it |
| `TZ`                      | `Asia/Tokyo`                    | container time zone (drives stored timestamps) |

The blocklist domains get their own database so a multi-million-entry list never
bloats the config file.

## API

Bottle on Waitress, JSON in and out. The web UI is built on this; see
`docs/frontend-integration.md` for response envelopes, error codes, and
per-endpoint shapes.

| area         | endpoints                                                                                             |
| ------------ | ---------------------------------------------------------------------------------------------------- |
| Health       | `GET /health` (process), `GET /ready` (database)                                                     |
| Policies     | `GET/POST /policies`, `GET/PUT/DELETE /policies/{id}`                                                 |
| Client modes | `GET/PUT /clients/{ip}/mode` — allow-all / block-all shortcut                                         |
| Hosts        | `GET /hosts`, `GET/PUT/DELETE /hosts/{id}`, `POST /hosts/{id}/sync` (Sando)                           |
| Upstreams    | `GET/POST /upstreams`, `PUT/DELETE /upstreams/{id}`, `POST /upstreams/{id}/check` (RTT probe)         |
| Blocklists   | `GET/POST /blocklists`, `GET/PUT/DELETE /blocklists/{id}`, `GET /blocklists/{id}/domains`, `GET /blocklists/search`, `POST /blocklists/{id}/refresh`, `GET/POST /blocklists/overrides`, `DELETE /blocklists/overrides/{id}` |
| Query log    | `GET /queries` (paginated, `?search=` by client or domain), `DELETE /queries`                        |
| Recent queries | `GET /clients/{ip}/queries` — live per-client request/response events (`?seconds=`, `?limit=`)     |
| Stats        | `GET /stats`, `GET /stats/site`, `GET /stats/new-domains`, `GET /stats/new-domains/recent`, `GET /stats/runtime`, `GET /stats/upstreams` |
| Requests     | `GET /requests` — top `(client, domain, qtype)` objects                                              |
| Cache        | `POST /cache/flush`                                                                                   |
| Settings     | `GET/PUT /settings`                                                                                   |
| Trusted networks | `GET/POST /trusted-networks`, `GET/DELETE /trusted-networks/{id}` — source-subnet allowlist      |

List endpoints take optional `limit`/`offset`.

### Runtime settings

Stored in the database, read with `GET /settings`, changed with `PUT /settings`:
`cache_enabled`, `cache_max_ttl`, `cache_min_ttl`, `cache_max_entries`,
`forward_timeout`, `default_action`, `refresh_seconds`, `query_flush_seconds`,
`log_queries`, `ipv6_enabled`.

Because the DNS server re-reads settings on the `refresh_seconds` timer, edits
apply without a restart.

## How it's put together

- **DNS server** (`mirenai.workers.dns_server`) — serves UDP and TCP, applies
  policy, caches forwarded answers, tracks clients, and records stats. Its own
  process.
- **Blocklist downloader** (`mirenai.workers.blocklist_downloader`) — fetches each
  enabled blocklist when its interval elapses and stores the domains. Its own
  process.
- **API** (`mirenai.api`) — Bottle on Waitress; CRUD and stats.

All three run under supervisord in one container. Configuration state is SQLite
(`mirenai.db`); blocklist domains and known clients get their own SQLite files;
everything persists on the `mirenai-data` volume. The web UI ships from a separate
repository and talks to the API.

A few specifics worth knowing:

- **Caching.** Forwarded answers are cached by `name|qtype|qclass`. TTL comes from
  the response (SOA minimum for negative answers), clamped to `[cache_min_ttl,
  cache_max_ttl]`, and counts down on each hit. The cache is process-local to the
  DNS server, so `POST /cache/flush` records a request the server honours on its
  next refresh tick, and its size shows up in `GET /stats/runtime`.
- **Blocklists.** Hosts format (`0.0.0.0 ads.example.com`) and domain-only lines
  are both accepted; `#` comments, blank lines, and bare IPs are ignored. Listing
  `example.com` also blocks its subdomains.
- **Trusted networks.** With no `trusted-networks` rows the resolver answers every
  client. Add one or more source subnets (e.g. `10.2.10.0/24`, multiple allowed)
  and any query from outside them is dropped before parsing — no reply on UDP or
  TCP, and no host record — and counted under the `foreign` series in `GET /stats`
  and `GET /stats/site`. The list is reloaded on the `refresh_seconds` timer.
- **Overrides** return `A`/`AAAA` records built from the IP(s) in
  `override_response` — the common sinkhole case.
- **Timestamps** are stored and returned in the container's local time zone via
  SQLite `datetime('now', 'localtime')`, so set `TZ` to taste.

## Development

Python 3.12+, packaged with setuptools. Runtime dependencies: Bottle, Waitress,
SQLAlchemy 2, Pydantic 2, dnslib.

```bash
make install      # pip install .[dev]
make initdb       # create the SQLite schema
make test         # pytest
make lint         # ruff
make typecheck    # mypy (strict)

python -m mirenai.api_main                       # run the API
python -m mirenai.workers.dns_server             # run the DNS server
python -m mirenai.workers.blocklist_downloader   # run the blocklist downloader
```

## Related

- [Sando](https://github.com/mayberryjp/sando) — optional source of client device
  names and icons.
- `docs/frontend-integration.md` — the API contract the web UI is built against.

# Comment to reforce rebuild
