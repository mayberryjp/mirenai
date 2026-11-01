# Discord notifications: frontend specification

## Settings UI

Add a **Discord notifications** section to the existing settings page:

| Control | API field | Default |
| --- | --- | --- |
| Enable notifications toggle | `discord_notifications_enabled` | `false` |
| Webhook URL password input, with optional reveal button | `discord_webhook_url` | `""` |
| Query-type toggles, including A, AAAA, PTR and additional types | `discord_query_types` | all recognized types `true` |

- Load using `GET /settings`; all three fields are always present in `settings`.
- Keep the URL editable when disabled, so users can configure before enabling.
- Trim the URL. Require a nonempty URL to enable.
- Save only changed fields using `PUT /settings`.
- Disabling preserves the URL unless explicitly cleared.
- To clear an enabled webhook, send both disable and empty URL in one request.
- Mask the URL by default. Never send it to analytics, console logs, error
  reporting, or browser persistent storage.
- Show pending/saving and error states; update saved values from the returned
  settings object, not optimistic assumptions.
- Success copy: **"Notification settings saved"**, not "Webhook connected".
  There is no test-webhook or delivery-status endpoint in this feature.
- Helper text: "Notify on a monitored client's first eligible query for a domain.
  Includes allow/block status. Disabled query types do not consume eligibility."

### Read

```http
GET /settings
```

```json
{
  "status": "ok",
  "settings": {
    "discord_notifications_enabled": false,
    "discord_webhook_url": "",
    "discord_query_types": {
      "A": true,
      "AAAA": true,
      "PTR": true,
      "MX": true,
      "TXT": true
    }
  }
}
```

The response above is abbreviated: the actual response includes all recognized
query types and all existing runtime settings. Preserve them in frontend types;
do not assume these are the only keys.

### Configure and enable

```http
PUT /settings
Content-Type: application/json
```

```json
{
  "discord_webhook_url": "https://discord.com/api/webhooks/123456/YOUR_WEBHOOK_TOKEN",
  "discord_notifications_enabled": true
}
```

Success is `200`:

```json
{
  "status": "ok",
  "settings": {
    "discord_notifications_enabled": true,
    "discord_webhook_url": "https://discord.com/api/webhooks/123456/YOUR_WEBHOOK_TOKEN"
  }
}
```

Again, `settings` contains all runtime settings. A successful save persists the
configuration; it does not verify the webhook's existence or permissions.

### Disable, retaining the URL

```json
{ "discord_notifications_enabled": false }
```

### Disable and clear

```json
{
  "discord_notifications_enabled": false,
  "discord_webhook_url": ""
}
```

### Validation/errors

- All three Discord fields reject `null`.
- URLs must use HTTPS on exactly `discord.com` or `discordapp.com`.
- Allowed paths: `/api/webhooks/{numeric_id}/{token}` or
  `/api/vN/webhooks/{numeric_id}/{token}`. Tokens contain letters, digits, `_`,
  or `-`. Query strings, fragments, credentials, custom ports, and trailing
  slashes are not accepted. Whitespace around the URL is trimmed.
- Enabling requires a nonempty URL in the **resulting** configuration. A saved
  URL need not be resubmitted when toggling on.
- Clearing while enabled is rejected unless the same request disables.
- Invalid fields, URLs, or combinations return `422 validation_error` with no
  partial save. Malformed JSON returns `400 bad_request`.
- Surface the API's `detail` near the form; retain unsaved user input.

### Query-type controls

Render one toggle per key returned in `settings.discord_query_types`. Put
**A**, **AAAA (IPv6)**, and **PTR (reverse DNS)** first; show the additional
types in an expandable/searchable section. The API provides flags for all
recognized types, including MX, TXT, CNAME, NS, SOA, SRV, CAA, HTTPS, SVCB,
and DNSSEC types. No separate type-discovery endpoint is needed.

Suggested frontend type:

```typescript
discord_query_types: Record<string, boolean>;
```

Save only changed flags:

```http
PUT /settings
Content-Type: application/json
```

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

Success returns `200 {"status": "ok", "settings": {...}}` with the full
effective flag map. Updates merge individual entries, so omitting CNAME, for
example, leaves its current setting unchanged. An empty map is a no-op, not
a reset. Sending every returned flag as `false` disables all recognized types.
Unrecognized numeric types remain enabled unless explicitly configured.

Names are trimmed and uppercased. Use `AAAA`, not `AAA`. Numeric query types
can be configured as `TYPE<number>` for codes 0 through 65535; recognized codes
are canonicalized (`TYPE1` becomes `A`). Configured numeric keys are also returned
by `GET /settings`. Unknown names and duplicate normalized names are rejected.
Values must be JSON `true` or `false`, not strings, numbers, or `null`;
invalid maps return `422 validation_error` without applying any part of the
settings update.

Keep query-type controls editable while global notifications are disabled.
Disabling all type flags is valid. These controls affect only Discord alerting,
not DNS resolution, query recording, or the recent new-domain feed.

Helper text: "Only selected query types can trigger an alert. If a domain is
first queried using an unselected type, its first later selected-type query
can still alert. Each client/domain pair alerts once, not once per type."

Example:

```json
{
  "status": "error",
  "code": "validation_error",
  "error": "Invalid request",
  "detail": "discord_webhook_url is required when notifications are enabled"
}
```

## Per-client monitoring UI

Use the existing host monitoring toggle, labeled **"New-domain monitoring"**.
It controls both the recent new-domain feed and Discord alerts, independently
of global notification enablement.

Read `new_domain_monitoring` from `GET /hosts` or `GET /hosts/{id}`. Update by
**host ID**, not IP:

```http
PUT /hosts/42
Content-Type: application/json
```

```json
{ "new_domain_monitoring": false }
```

Success is `200 {"status": "ok", "host": {...}}`. The full host object includes
both `new_domain_monitoring: false` and `flag_new_domains: false`.

`flag_new_domains` remains supported for old frontends; both names read/write
the same persisted flag. Defaults to `true`, including automatically discovered
clients. Send one name per update. If both non-null values are provided, they
must agree or the API returns `422`. `null` is ignored. Missing hosts return
`404 not_found`.

Helper text: "Show this client's newly seen domains and send Discord alerts
when global notifications are enabled."

## Notification behavior

- Identity is `(client IP, normalized domain)`, not query type. Case and trailing
  dots are normalized by the resolver.
- The first eligible query determines message details. Repeated queries,
  A/AAAA changes, cache flushes, and policy changes do not generate another
  discovery alert. Another client querying the same domain does.
- Queries of disabled types do not consume eligibility. A pair first queried
  using a disabled type can alert on its first later enabled-type query, using
  that query's action, response code, and local timestamp. This eligibility
  persists across batches and restarts.
- Enabling a type alone never replays prior queries; a new eligible observation
  is required. Disabling a type suppresses pending deliveries of that type
  (except HTTP already in flight), leaving the pair eligible for a later query.
- Existing request history is baselined without alerts when the DNS process
  starts. First-seen records persist across restarts.
- Pairs observed with global notifications or client monitoring disabled are
  recorded without alerting and are not replayed on enable.
- Disabling suppresses pending deliveries too. A request already in flight
  cannot be recalled. Changing the URL routes remaining deliveries to the
  currently configured webhook.
- Capture uses the DNS settings snapshot for global enable and query-type flags,
  refreshed within `refresh_seconds`
  (default 10 seconds). Background persistence/delivery checks current settings
  and host exclusions on its one-second tick, subject to HTTP/backoff delays.
- No alerts for untrusted-network drops or intentionally unlogged private PTR
  queries. This does not change DNS filtering or resolution.
- Messages are asynchronous, not tied to the hourly feed refresh. No restart
  is required to edit notification settings.
- Deleting a host clears its notification ledger, so future observations can
  be new again.

### Discord message

Messages are one sentence with an outcome icon and Discord bold formatting:

> 🛑 **BLOCK**: **SANDO CONTAINER (10.4.40.40)** queried **ads.example.com** for the first time (A; action: blocklist; DNS: NXDOMAIN; first seen: 2026-11-01T10:15:00).

Allowed queries use ✅ **ALLOW**; DNS errors use ⚠️ **ERROR**. The client
label uses the current saved `device_name` at delivery time (populated from
Sando's `local_description`, or edited through the hosts API), followed by its IP.
Missing or blank names show only the IP. Names have whitespace collapsed and
Markdown escaped so they cannot alter the message layout. No Sando HTTP lookup
is required to deliver an alert.

| Result | Meaning |
| --- | --- |
| `BLOCK` | Policy `deny` or `blocklist` filtering |
| `ALLOW` | Non-filtered response, including forward, forward-cache, local, override, NODATA, and upstream NXDOMAIN |
| `ERROR` | Non-filtered DNS error such as SERVFAIL or REFUSED |

`ALLOW` means not filtered, **not** necessarily an address was returned.
`BLOCK` queries still receive an NXDOMAIN DNS reply. The separate action and
response-code fields distinguish these cases. Timestamps use server/container
local time. Mentions are disabled in Discord payloads.

### Delivery limitations

- At most one outbound message per second, with a five-second HTTP timeout.
- Up to five attempts for network errors, HTTP 429, or HTTP 5xx, with backoff.
  `Retry-After` is respected up to one hour. Other HTTP failures are terminal.
- Pending persisted deliveries survive restarts. Failed deliveries are logged
  without exposing the webhook token; no delivery status is exposed to the UI.
- Best-effort delivery, not an exactly-once guarantee: abrupt termination can
  lose captures before persistence, a full 10,000-observation in-memory buffer logs and
  drops new observations, and ambiguous HTTP success or interruption before
  recording success can cause duplicates.

## Frontend acceptance criteria

1. A fresh install renders notifications disabled and the URL empty.
2. Saving URL + enabled succeeds; reopening settings restores the saved values.
3. Disable preserves the URL; disable + clear removes it.
4. Invalid URLs or enabling without a URL show an inline error without losing
   form input or claiming success.
5. A monitored client's first pair generates an asynchronous Discord message
   with the first eligible query's client/domain, outcome, action, qtype, and response code.
6. Repeating the domain with another query type does not create another alert;
   querying from another client can.
7. Either monitoring field set to `false` suppresses that client's alerts.
   Changing either name updates both returned fields and the feed behavior.
8. Enabling does not replay historical domains or disabled-period discoveries.
9. Existing settings and host editing flows remain functional.
10. No webhook token appears in frontend logs, analytics, or persistent storage.
11. Query-type controls load all returned flags, defaulting to enabled; A, AAAA,
    PTR, and additional DNS types can be independently toggled.
12. Updating PTR alone preserves A/AAAA and other saved flags after reopening.
13. A disabled-type first query sends no alert; a later enabled-type query for
    the same pair sends one with that later query's details, even after restart.
14. Enabling a type does not alert until a new eligible query occurs.
15. Invalid flag names/values show validation errors without partial saves.

For all other endpoint shapes, see [frontend integration](frontend-integration.md).
