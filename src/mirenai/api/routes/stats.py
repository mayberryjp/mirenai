from typing import Any

from bottle import Bottle, request

from mirenai.api.errors import read_int_query, read_pagination
from mirenai.repository import blocklists as blocklists_repo
from mirenai.repository import cache_outcome as cache_outcome_repo
from mirenai.repository import client_requests as requests_repo
from mirenai.repository import client_stats as repo
from mirenai.repository import hosts as hosts_repo
from mirenai.repository import runtime_stats as runtime_repo
from mirenai.repository import upstream_stats as upstream_repo
from mirenai.repository.upstreams import load_upstreams


def register_stats_routes(app: Bottle) -> None:
    @app.get("/stats")
    def list_stats() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        hours, err = read_int_query("hours")
        if err is not None:
            return err
        client = request.query.get("client") or None
        if hours is not None and client is not None:
            # gap-filled per-client series for graphs; limit/offset ignored
            rows = repo.list_client_stats(client=client, hours=hours, fill=True)
            return {"status": "ok", "stats": rows, "total": len(rows)}
        if not paginate:
            rows = repo.list_client_stats(client=client, hours=hours)
            return {"status": "ok", "stats": rows, "total": len(rows)}
        rows = repo.list_client_stats(limit=limit, offset=offset, client=client, hours=hours)
        total = repo.count_client_stats(client=client, hours=hours)
        return {"status": "ok", "stats": rows, "total": total}

    @app.get("/stats/site")
    def site_stats() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        hours, err = read_int_query("hours")
        if err is not None:
            return err
        if hours is not None:
            # gap-filled site-wide series for graphs; limit/offset ignored
            rows = repo.list_site_hourly_stats(hours=hours, fill=True)
            return {"status": "ok", "stats": rows, "total": len(rows)}
        if not paginate:
            rows = repo.list_site_hourly_stats()
            return {"status": "ok", "stats": rows, "total": len(rows)}
        rows = repo.list_site_hourly_stats(limit=limit, offset=offset)
        total = repo.count_site_hourly_stats()
        return {"status": "ok", "stats": rows, "total": total}

    @app.get("/stats/cache-outcomes")
    def cache_outcome_stats() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        hours, err = read_int_query("hours")
        if err is not None:
            return err
        if hours is not None:
            # gap-filled forwarded cache-outcome series for graphs; limit/offset ignored
            rows = cache_outcome_repo.list_cache_outcomes(hours=hours, fill=True)
            return {"status": "ok", "stats": rows, "total": len(rows)}
        if not paginate:
            rows = cache_outcome_repo.list_cache_outcomes()
            return {"status": "ok", "stats": rows, "total": len(rows)}
        rows = cache_outcome_repo.list_cache_outcomes(limit=limit, offset=offset)
        total = cache_outcome_repo.count_cache_outcomes()
        return {"status": "ok", "stats": rows, "total": total}

    @app.get("/stats/new-domains")
    def new_domain_stats() -> dict[str, Any]:
        client = request.query.get("client") or None
        rows = requests_repo.list_new_domain_counts(client=client)
        return {"status": "ok", "stats": rows, "total": len(rows)}

    @app.get("/stats/new-domains/recent")
    def recent_new_domains() -> dict[str, Any]:
        limit, err = read_int_query("limit")
        if err is not None:
            return err
        rows = requests_repo.list_recent_new_domains(
            limit=limit if limit is not None else 100,
            exclude_clients=hosts_repo.load_new_domain_unmonitored(),
        )
        blocklists_repo.annotate_blocked(rows)
        return {"status": "ok", "domains": rows, "total": len(rows)}

    @app.get("/stats/runtime")
    def runtime_stats() -> dict[str, Any]:
        return {"status": "ok", **runtime_repo.load_runtime_stats()}

    @app.get("/stats/upstreams")
    def upstream_rtt() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        hours, err = read_int_query("hours")
        if err is not None:
            return err
        address = request.query.get("address") or None
        if hours is not None:
            # Include every configured upstream so newly-added / idle ones still graph.
            known = None if address is not None else [u.address for u in load_upstreams()]
            rows = upstream_repo.list_upstream_rtt(
                address=address, hours=hours, fill=True, known_addresses=known
            )
            return {"status": "ok", "stats": rows, "total": len(rows)}
        if not paginate:
            rows = upstream_repo.list_upstream_rtt(address=address)
            return {"status": "ok", "stats": rows, "total": len(rows)}
        rows = upstream_repo.list_upstream_rtt(limit=limit, offset=offset, address=address)
        return {"status": "ok", "stats": rows, "total": upstream_repo.count_upstream_rtt(address=address)}
