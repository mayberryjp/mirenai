# mirenai — Frontend Integration Spec

This document describes the management HTTP API exposed by mirenai for the
frontend/UI. It is the single source of truth for endpoints, request/response
shapes, validation rules, and error handling.

The API is served by **Bottle + Waitress** from `mirenai.api`. All payloads are
JSON.

---

## 1. Base URL & transport

- **Protocol:** HTTP (plain; TLS is expected to be terminated by a reverse proxy in production).
- **Host/port:** bound to `API_LISTEN_ADDRESS` (default `0.0.0.0`) and `API_PORT` (default `8000`).
- **Default base URL (local):** `http://localhost:8000`
- **Content type:** send `Content-Type: application/json` on all bodies. All responses are `application/json`.
- **Paths are exact** — do not rely on trailing-slash redirects. Use the paths exactly as documented (e.g. `/policies`, not `/policies/`).

---

## 2. Authentication & CORS

- **No authentication.** There are no API keys, tokens, cookies, or auth headers. Every endpoint is open. (Access control is assumed to be handled at the network/proxy layer.)
- **CORS is fully open.** Every response includes:
  - `Access-Control-Allow-Origin: *`
  - `Access-Control-Allow-Methods: GET, POST, PUT, PATCH, DELETE, OPTIONS`
  - `Access-Control-Allow-Headers: *`
- **Preflight:** `OPTIONS` on any path returns `200` with an empty body. Browser preflight requests will succeed automatically.

---

## 3. Response envelope conventions

Every JSON response carries a `status` field: `"ok"` or `"error"`.

**Single resource (success):**
```json
{ "status": "ok", "policy": { /* resource object */ } }
```
The resource key is the singular resource name: `policy`, `upstream`, `blocklist`, `settings`, `host`.

**Collection (success):**
```json
{ "status": "ok", "policies": [ /* array */ ], "total": 42 }
```
The collection key is the plural resource name: `policies`, `upstreams`, `blocklists`, `queries`, `domains`, `hosts`, `stats`, `requests`. `total` is described in [§5 Pagination](#5-pagination).

**Delete (success):**
```json
{ "status": "ok", "deleted": 7 }
```
> **Note the differing meaning of `deleted`:**
> - For `DELETE /policies/{id}`, `/upstreams/{id}`, `/blocklists/{id}`, `/hosts/{id}` → `deleted` is the **id** of the removed row.
> - For `DELETE /queries` → `deleted` is the **number of rows** removed.

**Error:**
```json
{ "status": "error", "code": "validation_error", "error": "Invalid request", "detail": "client: value is not a valid IPv4 or IPv6 address" }
```
`detail` is optional and only present for some errors (validation, bad JSON, download failure).

---

## 4. Error model

All errors use the envelope above. Map on `code` (stable string), not on the human-readable `error` message.

| HTTP status | `code`             | When it happens                                                                 |
| ----------- | ------------------ | ------------------------------------------------------------------------------- |
| `400`       | `bad_request`      | Body is not valid JSON, or is not a JSON object.                                |
| `404`       | `not_found`        | Resource id does not exist, or unknown route.                                   |
| `409`       | `conflict`         | Uniqueness violation (duplicate policy `client`+`domain`, or blocklist `name`). |
| `422`       | `validation_error` | Body failed validation (bad field, wrong type, missing required, unknown field, non-integer pagination). |
| `502`       | `download_failed`  | `POST /blocklists/{id}/refresh` could not fetch the source URL. `detail` explains why. |
| `503`       | `not_ready`        | `GET /ready` only — database not reachable.                                     |
| `500`       | `internal_error`   | Unhandled server error.                                                         |

**Validation `detail` format:** `"<field>: <message>"`, e.g. `"port: port must be between 1 and 65535"`. Unknown fields produce `"<field>: Extra inputs are not permitted"`. Only the **first** validation error is reported.

---

## 5. Pagination

List endpoints (`/policies`, `/upstreams`, `/blocklists`, `/blocklists/{id}/domains`, `/queries`, `/hosts`, `/stats`, `/stats/site`, `/stats/upstreams`, `/requests`) accept **optional** `limit` and `offset` query parameters.

- **Neither supplied →** all rows are returned; `total` equals the number of rows in the response.
- **Either supplied →** results are paginated; `total` is the **full count** across all rows (not the length of this page).
  - `offset` defaults to `0` when only `limit` is given.
  - `limit` may be omitted while `offset` is present.
- **Non-integer `limit`/`offset` →** `422 validation_error`, `detail: "limit and offset must be integers"`.

Example: `GET /policies?limit=25&offset=50` → up to 25 policies starting at row 51, with `total` = total number of policies.

Ordering is fixed per resource (documented per endpoint below); there is no sort parameter.

A few list endpoints also accept resource-specific **filter** parameters (documented with the endpoint): `/stats` accepts `client` and `hours`; `/stats/site` accepts `hours`; `/requests` accepts `client`. Filters combine with `limit`/`offset`, and `total` reflects the filtered count.

---

## 6. Data formats

- **Timestamps** are ISO-8601 strings **without timezone offset**, at seconds precision, e.g. `"2026-09-25T14:30:00"`. They are **container local wall-clock time** (`TZ`, currently `Asia/Tokyo`), **not UTC**. Do **not** append `Z` or treat them as UTC. Nullable timestamp fields (e.g. `last_downloaded_at`) are `null` until set.
- **Booleans** are real JSON booleans (`true`/`false`).
- **`override_response`** is a string of one or more comma-separated IP addresses (e.g. `"10.0.0.53"` or `"10.0.0.53,10.0.0.54"`).
- **Integers/floats** are JSON numbers (`forward_timeout` is a float; all others integer).

---

## 7. Endpoints

### 7.1 Health & readiness

#### `GET /health`
Liveness. Always `200` when the process is up.
```json
{ "status": "ok", "service": "mirenai-api" }
```

#### `GET /ready`
Readiness — checks the database is reachable.
- `200`: `{ "status": "ok" }`
- `503`: `{ "status": "error", "code": "not_ready", "error": "<detail>" }`

---

### 7.2 Policies

A policy screens one `(client, domain)` pair. Precedence: most specific domain wins (exact > `*.suffix` > `*`); ties favour an exact client over `*`. When nothing matches, the `default_action` setting applies.

**Policy object:**
| field               | type              | notes                                                        |
| ------------------- | ----------------- | ------------------------------------------------------------ |
| `id`                | int               | server-assigned                                              |
| `client`            | string            | source IPv4/IPv6, or `"*"` for any client                    |
| `domain`            | string            | exact name, `"*.suffix"` (name + subdomains), or `"*"`       |
| `action`            | string            | one of `forward`, `override`, `deny`, `blocklist`            |
| `override_response` | string \| null    | comma-separated IP(s); required when `action = "override"`   |
| `override_ttl`      | int               | TTL for override answers; default `300`                      |
| `enabled`           | bool              | default `true`                                               |
| `description`       | string \| null    | free text                                                    |
| `created_at`        | string (datetime) |                                                              |
| `updated_at`        | string (datetime) |                                                              |

Ordering: by `id` ascending.

#### `GET /policies`
List. Supports pagination. → `{ "status": "ok", "policies": [...], "total": N }`

#### `GET /policies/{id}`
→ `200 { "status": "ok", "policy": {...} }` or `404 not_found`.

#### `POST /policies`
Create. → `201 { "status": "ok", "policy": {...} }`

Request body:
```json
{
  "client": "10.0.0.20",
  "domain": "*.internal.example",
  "action": "override",
  "override_response": "10.0.0.53",
  "override_ttl": 300,
  "enabled": true,
  "description": "internal split-horizon"
}
```
Required: `client`, `domain`, `action`. Others optional (defaults: `override_ttl=300`, `enabled=true`, `override_response=null`, `description=null`).

Validation:
- `client`: must be a valid IP address **or** `"*"`.
- `domain`: must not be empty/blank.
- `action`: must be one of `forward`, `override`, `deny`, `blocklist`.
- If `action = "override"`, `override_response` must be a non-empty string.
- Unknown fields are rejected (`422`).

Errors: `422 validation_error`, `409 conflict` (a policy for this `client`+`domain` already exists).

#### `PUT /policies/{id}`
Partial update — send only the fields you want to change. → `200 { "status": "ok", "policy": {...} }`

All fields optional; same validators as create apply to supplied fields. Note the create-only cross-field rule (override requires `override_response`) is **not** re-enforced on update, so when switching a policy to `override` include `override_response` in the same request.

Errors: `422 validation_error`, `404 not_found`, `409 conflict`.

#### `DELETE /policies/{id}`
→ `200 { "status": "ok", "deleted": <id> }` or `404 not_found`.

---

### 7.3 Upstreams

Upstream DNS resolvers used for `forward` answers. Consulted in priority order (lower `priority` value first, then `id`).

**Upstream object:**
| field        | type              | notes                                  |
| ------------ | ----------------- | -------------------------------------- |
| `id`         | int               |                                        |
| `name`       | string \| null    | optional label                         |
| `address`    | string            | IPv4/IPv6 address                      |
| `port`       | int               | 1–65535; default `53`                  |
| `protocol`   | string            | `udp` or `tcp`; default `udp`          |
| `enabled`    | bool              | default `true`                         |
| `priority`   | int               | lower = tried first; default `100`     |
| `created_at` | string (datetime) |                                        |
| `updated_at` | string (datetime) |                                        |

Ordering: by `priority` ascending, then `id`.

#### `GET /upstreams`
List, paginated. → `{ "status": "ok", "upstreams": [...], "total": N }`

#### `GET /upstreams/{id}`
→ `200 { "status": "ok", "upstream": {...} }` or `404`.

#### `POST /upstreams`
Create. → `201 { "status": "ok", "upstream": {...} }`
```json
{ "name": "cloudflare", "address": "1.1.1.1", "port": 53, "protocol": "udp", "enabled": true, "priority": 100 }
```
Required: `address`. Validation: `address` must be a valid IP; `port` 1–65535; `protocol` in `{udp, tcp}`. Unknown fields rejected. Errors: `422` (no `409` — upstreams have no uniqueness constraint; duplicates are allowed).

#### `PUT /upstreams/{id}`
Partial update. → `200` or `404`. Same validators on supplied fields.

#### `DELETE /upstreams/{id}`
→ `200 { "status": "ok", "deleted": <id> }` or `404`.

#### `POST /upstreams/{id}/check`
Probe the upstream: sends a sample `A` query (for `example.com`) through it and
returns the round-trip time. → `200 { "status": "ok", "rtt_ms": 12.3 }`, or `404`
if the id is unknown. If the upstream can't be reached or its reply can't be
parsed (timeout, connection refused, ...), returns `502 upstream_error` with the
reason in `detail`. Any reply counts as reachable, even an error rcode like
`SERVFAIL`. No request body; the probe uses the upstream's own `protocol` and the
current `forward_timeout` setting.

---

### 7.4 Blocklists

A blocklist is a downloadable list of domains to block. The DNS server blocks a
listed domain and its subdomains, but only for clients whose policy uses the
`blocklist` action. Blocklist **config** is returned here; the downloaded
**domains** are a separate sub-resource.

**Blocklist object:**
| field                   | type              | notes                                              |
| ----------------------- | ----------------- | -------------------------------------------------- |
| `id`                    | int               |                                                    |
| `name`                  | string            | unique                                             |
| `url`                   | string            | http(s) source URL                                 |
| `update_interval_hours` | int               | fetch cadence, ≥ 1; default `24`                   |
| `enabled`               | bool              | default `true`                                     |
| `domain_count`          | int               | number of domains stored (server-maintained)       |
| `last_downloaded_at`    | string \| null    | datetime of last successful download, else `null`  |
| `last_status`           | string \| null    | e.g. `"ok: 12345 domains"`, else `null`            |
| `format`                | string \| null    | detected source syntax: `hosts`, `domain`, `adblock`, or `unknown`; `null` until first download |
| `created_at`            | string (datetime) |                                                    |
| `updated_at`            | string (datetime) |                                                    |

Ordering: by `id` ascending. `domain_count`, `last_downloaded_at`, `last_status`, and `format` are read-only (maintained by the downloader) — they are ignored if sent in a write body (and unknown extras are rejected).

#### `GET /blocklists`
List, paginated. → `{ "status": "ok", "blocklists": [...], "total": N }`

#### `GET /blocklists/{id}`
→ `200 { "status": "ok", "blocklist": {...} }` or `404`.

#### `POST /blocklists`
Create. → `201 { "status": "ok", "blocklist": {...} }`
```json
{ "name": "StevenBlack hosts", "url": "https://raw.githubusercontent.com/StevenBlack/hosts/master/hosts", "update_interval_hours": 24, "enabled": true }
```
Required: `name`, `url`. Validation: `name` non-empty; `url` must be an `http`/`https` URL with a host; `update_interval_hours` ≥ 1. Errors: `422`, `409 conflict` (name already exists).

> Creating a blocklist does **not** download it immediately — the background downloader fetches it on its cadence. Use the refresh endpoint below to fetch now.

#### `PUT /blocklists/{id}`
Partial update. → `200` or `404`. Same validators; `409` if renaming to an existing name.

#### `DELETE /blocklists/{id}`
Deletes the blocklist **and all its stored domains**. → `200 { "status": "ok", "deleted": <id> }` or `404`.

#### `GET /blocklists/{id}/domains`
Lists the domains stored for this blocklist. Supports pagination. The `domains` array is an array of **plain strings**, not objects. Ordering: alphabetical by domain.
```json
{ "status": "ok", "domains": ["ads.example.com", "tracker.example.net"], "total": 132000 }
```
→ `404 not_found` if the blocklist id does not exist.

> Domain lists can be very large (tens/hundreds of thousands). **Always paginate** this endpoint in the UI.

#### `POST /blocklists/{id}/refresh`
Downloads the source URL immediately, parses it, and replaces the stored domains. Body is ignored. This call is **synchronous** and may take seconds for large lists.
- `200`: `{ "status": "ok", "blocklist": {...} }` — the returned object reflects the new `domain_count`, `last_downloaded_at`, `last_status`, and `format`.
- `404 not_found`: unknown id.
- `502 download_failed`: the fetch failed; `detail` carries the reason (e.g. `"HTTP 404"`, a timeout, or `"blocklist exceeds 67108864 bytes"`).

---

### 7.5 Query log

Per-client DNS query statistics, aggregated by `(client, domain, qtype)`.

**Query object:**
| field         | type              | notes                                         |
| ------------- | ----------------- | --------------------------------------------- |
| `id`          | int               |                                               |
| `client`      | string            | source IP                                     |
| `domain`      | string            | queried name                                  |
| `qtype`       | string            | DNS record type, e.g. `A`, `AAAA`, `MX`       |
| `count`       | int               | number of times seen                          |
| `last_action` | string \| null    | last action applied (`forward`/`override`/`deny`/`blocklist`) or `null` |
| `first_seen`  | string (datetime) |                                               |
| `last_seen`   | string (datetime) |                                               |

Ordering: by `last_seen` descending, then `id`.

#### `GET /queries`
List, paginated. → `{ "status": "ok", "queries": [...], "total": N }`

Optional `search=<text>` query param filters server-side to rows where **`client` OR `domain`** contains `<text>` (case-insensitive substring; `%`/`_` are matched literally). `total` reflects the filtered count, so pagination stays correct. Combine with `limit`/`offset` as usual.

#### `DELETE /queries`
Resets (deletes) **all** query statistics. → `{ "status": "ok", "deleted": <count> }` where `deleted` is the number of rows removed.

> This endpoint has no id — it clears the entire log. Confirm in the UI before calling.

---

### 7.6 Settings

Runtime settings live in the database and are re-read by the DNS server on a
timer, so changes take effect without a restart. `GET` returns the **effective**
settings (built-in defaults merged with any stored overrides), so all keys are
always present.

**Settings object:**
| key                  | type   | default  | notes                                             |
| -------------------- | ------ | -------- | ------------------------------------------------- |
| `cache_enabled`      | bool   | `true`   | enable/disable the in-memory DNS cache            |
| `cache_max_ttl`      | int    | `3600`   | upper clamp for cached TTLs (seconds)             |
| `cache_min_ttl`      | int    | `0`      | lower clamp for cached TTLs (seconds)             |
| `cache_max_entries`  | int    | `10000`  | LRU capacity                                      |
| `forward_timeout`    | float  | `5.0`    | upstream query timeout (seconds)                  |
| `default_action`     | string | `"forward"` | applied when no policy matches; `deny` or `forward` |
| `refresh_seconds`    | int    | `10`     | how often the DNS server reloads config           |
| `query_flush_seconds`| int    | `5`      | how often query stats flush to the database       |
| `log_queries`        | bool   | `true`   | enable/disable query logging                      |
| `ipv6_enabled`       | bool   | `true`   | when `false`, every AAAA (IPv6) query is answered `NOERROR`/NODATA so clients fall back to A |

#### `GET /settings`
→ `{ "status": "ok", "settings": { /* all keys above */ } }`

#### `PUT /settings`
Partial update — send only the keys you want to change. → `{ "status": "ok", "settings": { /* full effective settings after update */ } }`
```json
{ "cache_enabled": false, "default_action": "forward" }
```
Validation: `default_action` must be `deny` or `forward`. Unknown keys are rejected (`422`). There is no per-field range validation on the numeric settings beyond type — send sensible values. Always returns `200` with the full merged settings; there is no `404`.

---

### 7.7 Hosts

Client devices seen by the DNS server, stored in a separate `localhosts.db`
database. The DNS server auto-records one row per source IP on every query
(incrementing `query_count` and refreshing `last_seen`); there is **no create
endpoint**. The editable fields are `device_name`, `icon`, and
`excluded_from_blocklist`; everything else is server-maintained. When a client is
first seen it is also seeded with an explicit wildcard policy (see
[§7.10](#710-client-mode-simplified)) and, if a Sando API is configured, its
`device_name`/`icon`/`mac_address` are synced from Sando automatically.

**Blocklist filtering is global:** every client's DNS queries are filtered
through the enabled blocklists by default. Set `excluded_from_blocklist` to
`true` on a host to exempt that client entirely — none of its lookups are then
subject to the blocklist.

**Host object:**
| field         | type              | notes                                              |
| ------------- | ----------------- | -------------------------------------------------- |
| `id`          | int               |                                                    |
| `ip`          | string            | source IP (unique); server-recorded                |
| `device_name` | string \| null    | operator-assigned label; `null` until set          |
| `icon`        | string \| null    | icon key (e.g. from Sando); `null` until set        |
| `mac_address` | string \| null    | MAC address synced from Sando; `null` until set     |
| `excluded_from_blocklist` | bool  | when `true`, this client's queries bypass the blocklist (default `false`) |
| `query_count` | int               | total queries seen from this IP; server-maintained |
| `first_seen`  | string (datetime) | server-recorded                                    |
| `last_seen`   | string (datetime) | server-maintained                                  |

Ordering: by `last_seen` descending, then `id`.

#### `GET /hosts`
List, paginated. → `{ "status": "ok", "hosts": [...], "total": N }`

#### `GET /hosts/{id}`
→ `200 { "status": "ok", "host": {...} }` or `404 not_found`.

#### `PUT /hosts/{id}`
Set or clear the device name and/or icon, or toggle blocklist exclusion. → `200 { "status": "ok", "host": {...} }` or `404 not_found`.
```json
{ "device_name": "living-room-tv", "icon": "television_icon", "excluded_from_blocklist": true }
```
Validation: `device_name` and `icon` are each a string of at most 255 characters, or `null`. Whitespace is trimmed; an empty/blank string is stored as `null` (so sending `""` or `null` clears that field). `excluded_from_blocklist` is a boolean (`null` is ignored). Any field may be sent on its own. `ip`, `mac_address`, `query_count`, `first_seen`, `last_seen`, and `id` are read-only — sending any of them (or any other key) is rejected with `422`.

#### `POST /hosts/{id}/sync`
Sync this host's `device_name`, `icon` and `mac_address` from the configured Sando instance (looks the host's IP up in Sando and copies its friendly name, icon and MAC address). → `200 { "status": "ok", "host": {...} }` with the updated host.
- `404 not_found` — the host id does not exist, **or** Sando has no record for the host's IP (`detail` carries the IP).
- `503 not_configured` — no Sando API is configured (`SANDO_API_URL` is unset).
- `502 upstream_error` — Sando could not be reached or returned an unexpected reply; `detail` explains why.

#### `DELETE /hosts/{id}`
Completely removes the host: deletes the host row **and** every row keyed to its IP across the other tables — per-client hourly stats, request objects, query-log entries, and policies (including its client mode). → `200 { "status": "ok", "deleted": <id> }` or `404 not_found`. (The DNS server will re-create the host — and re-seed its default policy — on the device's next query.)

---

### 7.8 Client hourly stats

Per-client DNS query counts bucketed by wall-clock hour and broken down by
result. The DNS server aggregates these in memory and writes them **once an
hour**; buckets older than **500 hours** are purged automatically. This resource
is **read-only** — there are no create/update/delete endpoints.

**Stat object:**
| field        | type              | notes                                                       |
| ------------ | ----------------- | ----------------------------------------------------------- |
| `id`         | int               |                                                             |
| `hour_start` | string (datetime) | top of the hour this bucket covers, e.g. `"2026-09-25T20:00:00"` |
| `client`     | string            | source IP                                                   |
| `total`      | int               | all queries in the hour (≈ sum of the columns below)        |
| `forwarded`  | int               | answered by an upstream (`forward`)                         |
| `cached`     | int               | answered from cache (`forward-cache`)                       |
| `overridden` | int               | answered by a policy override (`override`)                  |
| `denied`     | int               | refused by policy — `NXDOMAIN` (`deny`)                     |
| `blocked`    | int               | refused by blocklist — `NXDOMAIN` (`blocklist`)             |
| `servfail`   | int               | upstream failure (`servfail`)                               |

"Approved" = `forwarded + cached + overridden`; "denied" = `denied + blocked`. `total` may exceed the sum of the columns if a future result type isn't itemized, so treat the columns as a breakdown of (not necessarily equal to) `total`.

Ordering: by `hour_start` descending, then `client`.

#### `GET /stats`
List, paginated. → `{ "status": "ok", "stats": [...], "total": N }`

**Filters (optional, combine with each other and with `limit`/`offset`):**
- `client=<ip>` — only rows for that client.
- `hours=<n>` — only buckets from the last `n` hours, i.e. `hour_start >= now - n hours`. Use this to fetch the **top N hours** precisely (e.g. `?hours=100` or `?hours=500`), since `limit` caps *rows* (which are per hour+client), not distinct hours. Non-integer `hours` → `422 validation_error`.

**Gap-filling for graphs:** when you pass **both** `client` and `hours` (a single client's time series), the response is filled to **one row per hour** across the whole window — hours with no data come back with every count `0` and `id: null` — so a per-client graph has no missing points. In this mode `limit`/`offset` are ignored, `total` is the number of hours returned, and the window is capped at 500 hours (the retention limit). Without `client`, `/stats` is not gap-filled (it can hold many clients per hour).

> The in-memory buffer is flushed hourly, so the **current** (in-progress) hour typically has no row until the top of the next hour, and the most recent row can be up to an hour behind. A clean shutdown flushes early; an abrupt kill can lose up to the current hour.

#### `GET /stats/site`
Site-wide hourly totals — the same counts **summed across all clients**, one row per hour (no `client`/`id`). Read-only, paginated. → `{ "status": "ok", "stats": [...], "total": N }` where `total` is the number of hours.

**Filter (optional):** `hours=<n>` — only the last `n` hours (same semantics as `/stats`). There is **no** `client` filter here (it is aggregated across every client). Non-integer `hours` → `422 validation_error`.

**Gap-filling for graphs:** when you pass `hours`, the response is filled to **one row per hour** across the whole window — hours with no queries come back with every count `0` (and `clients: 0`) — so the master graph has no missing points. In this mode `limit`/`offset` are ignored, `total` is the number of hours returned, and the window is capped at 500 hours (the retention limit).

**Site stat object** (note: no `id` or `client`; adds `clients`):
| field        | type              | notes                                              |
| ------------ | ----------------- | -------------------------------------------------- |
| `hour_start` | string (datetime) | top of the hour                                    |
| `total`      | int               | all queries that hour, across all clients          |
| `forwarded`  | int               | Σ `forward`                                        |
| `cached`     | int               | Σ `forward-cache`                                  |
| `overridden` | int               | Σ `override`                                       |
| `denied`     | int               | Σ `deny`                                           |
| `blocked`    | int               | Σ `blocklist`                                      |
| `servfail`   | int               | Σ `servfail`                                       |
| `clients`    | int               | number of distinct clients active that hour        |

Ordering: by `hour_start` descending. Example: `GET /stats/site?hours=168` for the last week of site-wide hourly totals.

#### `GET /stats/new-domains`
Counts of **newly-seen domains** per client, bucketed by wall-clock hour, over the
last **20 one-hour intervals**. A domain is "new" for a client in the hour it was
**first seen** (earliest `first_seen` across query types); each domain is counted
once per client. Read-only.

This is a **dense** series: the DNS worker materializes it hourly, writing a row
for **every known client for every one of the 20 hours** — hours with no new
domains come back with `new_domains: 0` (no gaps to fill client-side).

**Row object:**
| field         | type              | notes                                              |
| ------------- | ----------------- | -------------------------------------------------- |
| `hour_start`  | string (datetime) | top of the hour, e.g. `"2026-09-27T14:00:00"`      |
| `client`      | string            | source IP                                          |
| `new_domains` | int               | distinct domains this client first saw in the hour (0-filled) |

→ `{ "status": "ok", "stats": [...], "total": N }` where `total` is the number of rows (≈ known clients × 20 hours).

**Filter (optional):** `client=<ip>` — only that client's rows (a dense 20-row series for that client).

Ordering: by `hour_start` descending, then `client`. Because it's rebuilt hourly, the current in-progress hour and any brand-new client appear at the next materialize tick (up to ~1h lag). Example: `GET /stats/new-domains?client=10.0.0.5`.

---

#### `GET /stats/new-domains/recent`
The flat feed behind `GET /stats/new-domains`: the most recently first-seen
`(client, domain)` pairs, newest first. One row per `(client, domain)` dated by
the earliest `first_seen` across query types (so `a.com/A` and `a.com/AAAA`
collapse to a single row). Read-only.

**Row object:**
| field         | type              | notes                                                   |
| ------------- | ----------------- | ------------------------------------------------------- |
| `client`      | string            | source IP                                               |
| `domain`      | string            | the queried name                                        |
| `first_seen`  | string (datetime) | earliest time this client first saw this domain         |
| `last_action` | string \| null    | action last recorded for this `(client, domain)` in the query log (`forward`/`override`/`deny`/`blocklist`), or `null` |

→ `{ "status": "ok", "domains": [...], "total": N }` where `total` is the number of rows returned.

**Limit (optional):** `limit=<int>` — caps the number of rows (defaults to `100`, the top-100 most recent). A non-integer value returns `422`.

Ordering: by `first_seen` descending. Example: `GET /stats/new-domains/recent` (top 100) or `GET /stats/new-domains/recent?limit=25`.

#### `GET /stats/runtime`
A point-in-time snapshot of DNS-server runtime gauges (not time-bucketed). The
DNS server samples these every `query_flush_seconds` and writes them to the
database; this endpoint returns the latest values. Read-only.

→ `{ "status": "ok", "stats": { ... }, "updated_at": "2026-09-28T12:34:56" | null }`

`updated_at` is the time of the last flush and doubles as the DNS worker's
heartbeat — a recent value means the worker is alive. Before the first flush
(fresh install or DNS worker not yet running) `stats` is `{}` and `updated_at`
is `null`.

**Gauges (`stats` keys):**
| key                 | notes                                                                 |
| ------------------- | --------------------------------------------------------------------- |
| `cache_size`        | entries currently held in the in-memory DNS cache (one entry per `(name, type, class)` response, not individual records) |
| `cache_capacity`    | the cache's configured maximum entries (compare with `cache_size` for utilization) |
| `blocklist_domains` | number of domains loaded into the running resolver's blocklist        |
| `upstreams`         | number of configured upstream resolvers                               |
| `policies`          | number of policy rules loaded                                         |

Keys may be added over time — treat `stats` as an open map.

#### `GET /stats/upstreams`
Per-upstream forward round-trip time, bucketed by wall-clock hour — the feed for a
latency graph. The DNS server times each successful forward and aggregates by
`(hour, upstream address)`; buckets older than 500 hours are purged. Read-only.

**Row object:**
| field        | type              | notes                                                  |
| ------------ | ----------------- | ------------------------------------------------------ |
| `hour_start` | string (datetime) | top of the hour, e.g. `"2026-09-28T14:00:00"`          |
| `address`    | string            | upstream resolver address (e.g. `8.8.8.8`)             |
| `samples`    | int               | forwarded queries measured in that hour                |
| `avg_ms`     | float \| null     | mean round-trip time (ms); `null` when `samples` is 0  |
| `max_ms`     | float \| null     | worst round-trip time (ms); `null` when `samples` is 0 |

→ `{ "status": "ok", "stats": [...], "total": N }`, ordered newest hour first.

**Graph feed:** pass `hours=<n>` for a dense series — one row per hour for every
upstream across the window, with empty hours null-filled (`samples: 0`, `avg_ms:
null`, `max_ms: null`) so every line has a point at every hour even when an
upstream had no traffic. The upstream set is the union of every configured
(enabled) upstream and any address with history in the window, so a newly-added
or idle upstream still appears as a flat/empty line. `limit`/`offset` are ignored
in this mode. Add `address=<ip>` to restrict to a single upstream (works even if
it has no rows yet).

**Plain list:** without `hours`, returns recent rows (optionally `limit`/`offset`,
`total` is the matching row count), newest first — optionally filtered by `address`.

---

### 7.9 Client requests

Per-client DNS request objects (query names) with a hit counter, one row per
`(client, domain, qtype)`. The DNS server aggregates these in memory and writes
them in an **hourly batch**. This resource is **read-only** — there are no
create/update/delete endpoints.

**Request object:**
| field        | type              | notes                                            |
| ------------ | ----------------- | ------------------------------------------------ |
| `id`         | int               |                                                  |
| `client`     | string            | source IP                                        |
| `domain`     | string            | the queried name (the "request object")          |
| `qtype`      | string            | DNS record type, e.g. `A`, `AAAA`, `CNAME`, `MX` |
| `hits`       | int               | number of times this client queried this object  |
| `first_seen` | string (datetime) |                                                  |
| `last_seen`  | string (datetime) |                                                  |

Ordering: by `hits` descending, then `id` — so the most-requested ("principal") objects come first. `GET /requests?limit=100` returns the top 100.

#### `GET /requests`
List, paginated. → `{ "status": "ok", "requests": [...], "total": N }`

**Filter (optional):** `client=<ip>` — only that client's request objects. Combines with `limit`/`offset`; e.g. `?client=10.0.0.5&limit=10` returns that client's top 10 objects by hits.

> Like the query log, timestamps and hit counts reflect the hourly flush, so the most recent activity can be up to an hour behind. This table has no automatic retention cap — it grows with the number of distinct `(client, domain, qtype)` triples seen.

---

### 7.10 Client mode (simplified)

A convenience wrapper over policies for the common “what should this client do by
default?” toggle. A client's **mode** is just its wildcard policy row `(client,
"*", action)`; this endpoint reads and sets it in **one call**, so the UI never
has to list `/policies` or juggle `POST`/`PUT`/`DELETE`. The response is a flat
object (fields at the top level, not under a resource key).

There are exactly two modes — **allow all** and **block all**. Every client that
has been seen has an explicit wildcard row (seeded on first sight from the global
`default_action`), so switching modes just flips that row.

**Modes:**
| `mode`    | Meaning                                | Backing policy row         |
| --------- | -------------------------------------- | -------------------------- |
| `forward` | Allow all (forward every query)        | `(client, "*", "forward")` |
| `deny`    | Block all (`NXDOMAIN` for every query) | `(client, "*", "deny")`    |

**Mode object:**
| field       | type          | notes                                                         |
| ----------- | ------------- | ------------------------------------------------------------- |
| `client`    | string        | the IP from the path                                          |
| `mode`      | string        | one of the modes above                                        |
| `policy_id` | int \| null   | id of the backing wildcard policy, or `null` if none exists yet |

#### `GET /clients/{ip}/mode`
→ `200 { "status": "ok", "client": "10.4.10.20", "mode": "deny", "policy_id": 7 }`

`{ip}` must be a valid IP address (else `422 validation_error`). A client with no
wildcard row yet reports the global `default_action` as its `mode` with
`"policy_id": null`.

#### `PUT /clients/{ip}/mode`
Idempotent upsert — always `200` with the resulting mode object (no `201`, no `409`, so you never check whether the row already exists).
```json
{ "mode": "forward" }
```
- `mode` is required and must be `forward` (allow all) or `deny` (block all). An invalid mode or any unknown field → `422 validation_error`.
- Creates or updates the client's `(client, "*")` policy.

**Notes:**
- This only touches the client's **wildcard** row. Per-domain exceptions added via `/policies` (allow or deny a specific name for this client) are left untouched, still take precedence over the mode, and do **not** change the client's mode.
- For a **whitelist** client (allow only specific names), set the mode to `deny` (block all) and add per-domain `forward` rows via `/policies`.
- `GET` can report `"mode": "override"` or `"blocklist"` if a client's wildcard row was set to one of those via the advanced `/policies` API; `PUT` only accepts `forward`/`deny`.

---

### 7.11 Cache control

The DNS resolver keeps an in-memory cache of upstream answers (surfaced as the
`cache_size` / `cache_capacity` gauges on `GET /stats/runtime`).

#### `POST /cache/flush`
Requests a flush of the DNS cache. → `{ "status": "ok", "requested_at": "2026-09-28T12:34:56" }`

The cache lives in the DNS server process, so the flush is **not instantaneous**:
the API records the request and the DNS server clears its cache the next time it
polls, within `refresh_seconds` (default 10s). `requested_at` is the local-time
timestamp recorded for the request. No request body is needed; repeated calls
just update the pending request. If the DNS server isn't running there's nothing
to clear — a freshly started server always begins with an empty cache.

---

## 8. Enumerations reference

| Enum              | Allowed values                                      | Used by                      |
| ----------------- | --------------------------------------------------- | ---------------------------- |
| Policy `action`   | `forward`, `override`, `deny`, `blocklist`          | policies                     |
| Client `mode`     | `forward` (allow all), `deny` (block all)           | /clients/{ip}/mode           |
| Upstream `protocol` | `udp`, `tcp`                                      | upstreams                    |
| `default_action`  | `deny`, `forward`                                   | settings                     |

`override` requires a non-empty `override_response`. `blocklist` action forwards
everything except names on an enabled blocklist (those return `NXDOMAIN`).

---

## 9. Integration notes & gotchas

- **Treat timestamps as local wall-clock**, not UTC. They have no offset suffix. If you need correct ordering across DST or zones, rely on the server-provided ordering rather than reparsing.
- **Map errors on `code`, not `error` text.** The `error`/`detail` strings are for display and may change.
- **Creates that can 409:** policies (duplicate `client`+`domain`) and blocklists (duplicate `name`). Surface these as friendly "already exists" messages. Upstreams never 409.
- **`PUT` is a partial update** (send only changed fields). Sending `null` explicitly will set a nullable field to null; omitting a field leaves it unchanged.
- **Unknown fields are rejected** with `422` on every write endpoint — don't send extra keys (e.g. read-only blocklist stats, or `id`/timestamps).
- **Blocklist refresh is synchronous and can be slow/large;** show a spinner and handle `502 download_failed` with the returned `detail`.
- **`GET /blocklists/{id}/domains` can return huge arrays** — always paginate.
- **`DELETE /queries` clears everything** and returns a row count — guard it behind confirmation.
- **New blocklists aren't downloaded on create;** either wait for the downloader's cadence or call the refresh endpoint to populate `domain_count`.
- **No streaming/websockets.** Query stats and blocklist status update via polling; poll `GET /queries` and `GET /blocklists` at whatever interval suits the UI.
