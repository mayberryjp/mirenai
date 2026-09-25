# mirenai — TODO

Planned work and ideas. Check items off as they land.

## Features

- [ ] **"Override some, allow the rest" mode** — a per-client mode that returns
  override (sinkhole) answers for specific domains and forwards everything else
  upstream. This is the mirror of the existing whitelist mode ("allow some, deny
  the rest"). It is already expressible in the policy table as per-domain
  `override` rows plus a `(client, *, forward)` wildcard, so the work is to make
  it a recognized, first-class mode (naming, API/validation, UI, docs) alongside
  allow-all, deny-all, whitelist, and allow-all-but-blocklist.
