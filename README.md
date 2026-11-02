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
> storage, client DNS over UDP and TCP, and optional DNS-over-TLS upstreams.
> No external database or extra services to
> run.

## What it does

- **Per-client policies** — allow, deny, sinkhole, or blocklist a name, scoped to
  a single client IP or applied to everyone with `*`.
- **Different answers per client** — the same domain can forward for one host and
  return `NXDOMAIN` for another.
- **Sinkholes and overrides** — hand back your own A/AAAA records for a name: kill
  ad domains, pin an internal service, do split-horizon DNS.
- **Local DNS from a file** — point it at one or more plain-text files (e.g. raw
  GitHub URLs) of `ip_address,domain` lines; it fetches them on a schedule (and on
  demand), builds forward **and** reverse records (A/AAAA + PTR, or a CNAME when the
  value is a name), and answers them authoritatively from memory like real DNS.
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
- **Encrypted upstreams** — enable DNS-over-TLS per upstream, with mandatory
  certificate and hostname verification. Client devices can keep using normal DNS.
- **Real visibility** — per-client query counts, a searchable query log, a live
  per-client request/response tail, top policy-denied domains, a feed of newly-seen domains, and runtime
  gauges like cache size and blocklist size.
- **Device names, optionally** — sync friendly names and icons for each client
  from [Sando](https://github.com/mayberryjp/sando).
- **Discord notifications, optionally** — alert on the first eligible query for each
  client/domain pair, including its allow/block/error outcome; respects the
  client's new-domain monitoring switch.
- **One moving part** — the DNS server, blocklist and local-zone downloaders, and
  API run together under supervisord, backed by SQLite on a single volume.

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

**Spoof rules** are policies with `action = override`. List just these with
`GET /policies?action=override&limit=25&offset=0`; `total` is the filtered count,
including disabled rules. Create and edit them through `POST /policies` and
`PUT /policies/{id}`, toggle `enabled`, or remove them with `DELETE /policies/{id}`.
Override answers must be valid comma-separated IPv4/IPv6 addresses and TTLs must
be between 0 and 2147483647 seconds; partial edits validate the resulting rule
before saving. These rules synthesize A/AAAA answers, not arbitrary DNS record
types. Changes reach the DNS worker on its next `refresh_seconds` reload.

**Blocklists are global.** Enabled blocklists apply to *every* client's queries by
default, so you don't need a `blocklist` policy row to get filtering. To exempt a
device, set `excluded_from_blocklist` on its host. To exempt a single *domain* from
every blocklist, add it as an override (`POST /blocklists/overrides`); override domains
are stripped from each list when it is downloaded, so the exemption applies on the
list's next refresh rather than at query time.

**New-domain monitoring is per-client.** A client's newly-seen domains show up in
`GET /stats/new-domains/recent` by default; clear `flag_new_domains` on its host to
hide that client's new domains from the feed.
First-seen times are scoped to each `(client, domain)` pair: a domain already
seen by one client is still new when another client first queries it.
`new_domain_monitoring` is an API alias for `flag_new_domains`; setting either
to `false` also suppresses that client's Discord notifications.

**Discord alerts are opt-in.** Configure them through `PUT /settings`:

```json
{
  "discord_webhook_url": "https://discord.com/api/webhooks/123456/YOUR_WEBHOOK_TOKEN",
  "discord_notifications_enabled": true
}
```

Both fields are returned by `GET /settings`. Notifications default to disabled,
and enabling requires a webhook URL. Only HTTPS webhook URLs on `discord.com`
or `discordapp.com` are accepted (optional `/api/vN` version; no query string,
credentials, or custom port). Send `discord_notifications_enabled: false` to
disable without losing the URL; also send `discord_webhook_url: ""` to clear it.
The URL contains a secret token: protect API access and the configuration DB.

**Filter alerts by query type** with the `discord_query_types` boolean map:

```json
{
  "discord_query_types": {
    "A": true,
    "AAAA": true,
    "PTR": false,
    "MX": false,
    "TXT": true
  }
}
```

Send this to `PUT /settings`; entries are merged, so omitted types keep their
saved flags. `GET /settings` returns flags for every recognized DNS type,
all enabled by default (including CNAME, NS, SOA, SRV, CAA, HTTPS, and SVCB).
Names are case-insensitive; use `AAAA`, not `AAA`. Unrecognized numeric types
can be configured as `TYPE<number>` (0–65535); they default to enabled.
Flags must be JSON booleans. An empty map is a no-op; disable all returned
flags to suppress all query types.

Queries of disabled types do not consume the pair's alert eligibility. Its
first later enabled-type query generates the alert, with that query's action,
response code, and timestamp. Changing flags alone does not replay old queries.
Each pair still alerts only once, not once per query type. This filter affects
Discord only, not the new-domain feed or DNS resolution.

The DNS process captures normalized `(client IP, domain)` pairs independently of
query type and persists them in `stats.db` on a one-second background tick.
Discord HTTP never runs on the DNS query thread. Messages are a single sentence
with an outcome icon and bold client/domain labels. They include the client's
current saved device name (Sando description) followed by its IP, or just the IP
when unnamed, plus the domain, first eligible query type/action, DNS response
code, and local timestamp.
`BLOCK` means policy deny or blocklist filtering; `ALLOW` includes forwarded,
cached, local, override, and NODATA responses. An upstream `NXDOMAIN` is `ALLOW`
(not filtered); failed responses such as `SERVFAIL` are `ERROR`.
Untrusted-network drops and intentionally unlogged private PTR queries do not alert.

Existing request history is baselined on startup without sending alerts. Pairs
seen while notifications or client monitoring are disabled are not replayed
when enabled. Disabling also cancels pending alerts, except an HTTP request
already in flight. Notification settings are checked on each delivery; DNS
capture observes global enable changes on the usual `refresh_seconds` reload.

The worker sends at most one message per second, with up to five attempts for
network errors, HTTP 429, or HTTP 5xx (backoff, honoring `Retry-After` up to an
hour); permanent HTTP errors are not retried. Failures are logged without webhook
tokens. Persisted pending alerts survive restarts. Delivery is best-effort:
an abrupt stop can lose not-yet-persisted captures, a full 10,000-observation capture
buffer logs and drops new observations, and an ambiguous HTTP success or crash
before recording delivery can produce a duplicate. Deleting a host also resets
its first-seen notification ledger.

See [the Discord frontend spec](docs/discord-notifications-frontend.md) for UI
requirements and API examples.

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

### DNS-over-TLS upstreams

Set `use_tls` to `true` and supply the provider's `tls_server_name`:

```json
{
  "address": "1.1.1.1",
  "port": 853,
  "use_tls": true,
  "tls_server_name": "cloudflare-dns.com"
}
```

Send this to `POST /upstreams`, or update an existing upstream with
`PUT /upstreams/{id}`. TLS always uses TCP regardless of `protocol`; the server
name is required for SNI and certificate hostname verification, while `address`
remains the connection IP. The system CA trust store validates the certificate.
New TLS upstreams default to port 853 if omitted; partial updates preserve the
existing port, so specify 853 when enabling TLS on an existing port-53 upstream.
`use_tls` defaults to `false`, preserving existing UDP/TCP behavior.
Forwarding and health checks both use the configured transport. A TLS failure
never retries that upstream over plaintext; normal failover still tries other
enabled upstreams, so configure only TLS upstreams if encryption is required.
Each TLS query currently opens its own connection; connections are not pooled.
This encrypts only the upstream hop, not DNS traffic from devices to mirenai.

### Deployment settings

Environment variables cover deployment only; everything operational lives in the
database and is edited through the API. All of these are set in
`docker-compose.yml`:

| variable                  | default                         | purpose                                   |
| ------------------------- | ------------------------------- | ----------------------------------------- |
| `CONFIG_DATABASE_URL`     | `sqlite:////data/config.db`     | configuration database (policies, upstreams, blocklists, zones) |
| `STATS_DATABASE_URL`      | `sqlite:////data/stats.db`      | per-client stats and time-series          |
| `CACHE_DATABASE_URL`      | `sqlite:////data/cache.db`      | DNS answer-cache mirror                    |
| `QUERYLOG_DATABASE_URL`   | `sqlite:////data/querylogs.db`  | query log and recent query events         |
| `BLOCKLIST_DATABASE_URL`  | `sqlite:////data/blocklist.db`  | blocklist-domains database                |
| `LOCALHOSTS_DATABASE_URL` | `sqlite:////data/localhosts.db` | known-clients database                    |
| `API_LISTEN_ADDRESS`      | `0.0.0.0`                       | API bind address                          |
| `API_PORT`                | `8000`                          | API port                                  |
| `DNS_LISTEN_ADDRESS`      | `0.0.0.0`                       | DNS bind address                          |
| `DNS_PORT`                | `53`                            | DNS port (UDP + TCP)                      |
| `LOG_LEVEL`               | `INFO`                          | log level                                 |
| `SANDO_API_URL`           | *(empty)*                       | Sando base URL for name/icon sync; empty disables it |
| `TZ`                      | `Asia/Tokyo`                    | container time zone (drives stored timestamps) |

> On first start, if a pre-split `/data/mirenai.db` is present (the legacy `DATABASE_URL`, default
> `sqlite:////data/mirenai.db`), its data is copied into the four databases above and the file is
> renamed to `mirenai.db.migrated`.

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
| Blocklists   | `GET/POST /blocklists`, `GET/PUT/DELETE /blocklists/{id}`, `GET /blocklists/{id}/domains`, `GET /blocklists/search`, `GET /blocklists/recent` (newest entries by first-seen), `GET /blocklists/size-history` (hourly size), `POST /blocklists/{id}/refresh`, `GET/POST /blocklists/overrides`, `DELETE /blocklists/overrides/{id}` |
| Local zones  | `GET/POST /local-zones`, `GET/PUT/DELETE /local-zones/{id}`, `GET /local-zones/{id}/records`, `POST /local-zones/{id}/refresh`, `GET /local-records` |
| Query log    | `GET /queries` (paginated, `?search=` by client or domain, `?client=` exact IP), `GET /queries/top-blocked`, `GET /queries/top-denied` (actual policy-denial counts, `?limit=`, `?client=`), `DELETE /queries` |
| Recent queries | `GET /clients/{ip}/queries` — live per-client request/response events (`?seconds=`, `?limit=`)     |
| Stats        | `GET /stats`, `GET /stats/site`, `GET /stats/cache-outcomes`, `GET /stats/new-domains`, `GET /stats/new-domains/recent`, `GET /stats/runtime`, `GET /stats/upstreams` |
| Requests     | `GET /requests` — top `(client, domain, qtype)` objects                                              |
| Cache        | `POST /cache/flush`, `GET /cache/uncacheable` — forwarded answers that couldn't be cached, with reason + hit counts |
| Settings     | `GET/PUT /settings`                                                                                   |
| Trusted networks | `GET/POST /trusted-networks`, `GET/DELETE /trusted-networks/{id}` — source-subnet allowlist      |
| Foreign clients | `GET /foreign-clients` — source IPs dropped by the trusted-network screen (per-IP hit counts) |

List endpoints take optional `limit`/`offset`.

**Top policy-denied domains:** `GET /queries/top-denied?limit=10` returns domains
ranked by actual DNS queries resolved with action `deny`, including explicit
client/global policies and the site-wide default deny. Blocklist filtering,
upstream NXDOMAIN/errors, and allowed queries do not count. Rows include denial
counts, per-client denial counts, and first/last denial timestamps; an optional
`client=<ip>` filters before ranking. The default limit is 20; non-integer or
non-positive limits return `422`. `total` is the number of returned domains.

Denial counters are persisted separately in `querylogs.db`, on the existing
`query_flush_seconds` timer (default 5 seconds). Later allowed queries or policy
changes do not remove earlier denials. The new table is created automatically,
without altering existing query-log rows. Exact denial counts start when this
feature is deployed: historical all-action query counts cannot be safely
backfilled. `DELETE /queries` resets these counters too (its `deleted` value
still counts regular query-log rows); deleting a host removes its denial history.

### Runtime settings

Stored in the database, read with `GET /settings`, changed with `PUT /settings`:
`cache_enabled`, `cache_max_ttl`, `cache_min_ttl`, `cache_max_entries`,
`forward_timeout`, `default_action`, `refresh_seconds`, `query_flush_seconds`,
`log_queries`, `ipv6_enabled`, `drop_private_ptr`,
`discord_notifications_enabled`, `discord_webhook_url`, `discord_query_types`.

`drop_private_ptr` (on by default) answers reverse (`PTR`) lookups for RFC1918
private addresses with NODATA, and skips logging them, unless the address is
defined in a local zone — so noisy private-range reverse scans don't fill the logs.

Because the DNS server re-reads settings on the `refresh_seconds` timer, edits
apply without a restart.

## How it's put together

- **DNS server** (`mirenai.workers.dns_server`) — serves UDP and TCP, applies
  policy, caches forwarded answers, tracks clients, and records stats. Its own
  process.
- **Blocklist downloader** (`mirenai.workers.blocklist_downloader`) — fetches each
  enabled blocklist when its interval elapses and stores the domains. Also samples
  the total enabled-blocklist size into an hourly series (retained ~500 hours) for
  `GET /blocklists/size-history`. Its own process.
- **Local-zone downloader** (`mirenai.workers.local_zones`) — fetches each enabled
  local zone when its interval elapses and stores its parsed records. Its own
  process.
- **API** (`mirenai.api`) — Bottle on Waitress; CRUD and stats.

All four run under supervisord in one container. Configuration state is SQLite
(`mirenai.db`); blocklist domains and known clients get their own SQLite files;
everything persists on the `mirenai-data` volume. The web UI ships from a separate
repository and talks to the API.

A few specifics worth knowing:

- **Caching.** Forwarded answers are cached by `name|qtype|qclass`. TTL comes from
  the response — the answer's own TTL for positive replies, or the SOA MINIMUM for
  negative ones (NXDOMAIN/NODATA are negatively cached per RFC 2308) — clamped to
  `[cache_min_ttl, cache_max_ttl]`, and counts down on each hit. The cache is
  process-local to the DNS server, so `POST /cache/flush` records a request the server
  honours on its next refresh tick, and its size shows up in `GET /stats/runtime`.
  Answers that still can't be cached — a negative reply carrying no SOA, another error
  rcode, a zero TTL, or an upstream failure — are tallied by reason at
  `GET /cache/uncacheable` (per-domain detail) and bucketed hourly at
  `GET /stats/cache-outcomes` (a forwarded-reason chart) to help diagnose a low
  cache-hit rate.
- **Blocklists.** Hosts format (`0.0.0.0 ads.example.com`) and domain-only lines
  are both accepted; `#` comments, blank lines, and bare IPs are ignored. Listing
  `example.com` also blocks its subdomains.
- **Local zones.** Each zone is an http(s) file of `value,name` lines (`#` comments,
  full-line or inline, and blank lines are ignored; an optional third field sets the
  record TTL). An IP `value` produces an A/AAAA record **and** the matching PTR for
  reverse lookups; a hostname `value` produces a CNAME, which is chased to its
  address within the local set. Records from *enabled* zones are loaded into the DNS
  worker's memory and answered authoritatively — a name defined locally is never
  forwarded, returning NODATA for a type it doesn't carry instead of leaking to an
  upstream. Explicit policy (override/deny) and blocklists still take precedence.
  Zones refresh on each one's `update_interval_seconds` cadence (default 86400s,
  i.e. daily) or immediately via `POST /local-zones/{id}/refresh`; the loaded record
  count shows up as `local_records` in `GET /stats/runtime`.
- **Trusted networks.** With no `trusted-networks` rows the resolver answers every
  client. Add one or more source subnets (e.g. `10.2.10.0/24`, multiple allowed)
  and any query from outside them is dropped before parsing — no reply on UDP or
  TCP, and no host record — and counted under the `foreign` series in `GET /stats`
  and `GET /stats/site`. The offending source IPs are also recorded per-IP (with
  hit counts, capped to the most recent 1000) and listed by `GET /foreign-clients`.
  The list is reloaded on the `refresh_seconds` timer.
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
python -m mirenai.workers.local_zones            # run the local-zone downloader
```

## Related

- [Sando](https://github.com/mayberryjp/sando) — optional source of client device
  names and icons.
- `docs/frontend-integration.md` — the API contract the web UI is built against.

# Comment to reforce rebuild
