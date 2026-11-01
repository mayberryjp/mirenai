from typing import Any

from bottle import Bottle, request

from mirenai.api.errors import error, read_int_query, read_pagination
from mirenai.repository import blocklists as blocklists_repo
from mirenai.repository import query_log as repo

# Domains returned by GET /queries/top-blocked when no ?limit is given.
_DEFAULT_TOP_BLOCKED = 20
_DEFAULT_TOP_DENIED = 20


def register_query_routes(app: Bottle) -> None:
    @app.get("/queries")
    def list_queries() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        search = (request.query.get("search") or "").strip() or None
        client = (request.query.get("client") or "").strip() or None
        if not paginate:
            rows = repo.list_queries(search=search, client=client)
            blocklists_repo.annotate_blocked(rows)
            return {"status": "ok", "queries": rows, "total": len(rows)}
        rows = repo.list_queries(limit=limit, offset=offset, search=search, client=client)
        blocklists_repo.annotate_blocked(rows)
        return {
            "status": "ok",
            "queries": rows,
            "total": repo.count_queries(search=search, client=client),
        }

    @app.get("/queries/top-blocked")
    def top_blocked_domains() -> dict[str, Any]:
        limit, err = read_int_query("limit")
        if err is not None:
            return err
        top = limit if limit is not None and limit > 0 else _DEFAULT_TOP_BLOCKED
        totals = repo.aggregate_domain_totals()
        sources = blocklists_repo.find_domain_sources([row["domain"] for row in totals])
        blocked = [row for row in totals if row["domain"] in sources][:top]
        clients = repo.clients_for_domains([row["domain"] for row in blocked])
        for row in blocked:
            row["clients"] = clients.get(row["domain"], [])
            row["blocklists"] = sources[row["domain"]]
        return {"status": "ok", "domains": blocked, "total": len(blocked)}

    @app.get("/queries/top-denied")
    def top_denied_domains() -> dict[str, Any]:
        limit, err = read_int_query("limit")
        if err is not None:
            return err
        if limit is not None and limit < 1:
            return error("validation_error", "Invalid request", 422, "limit must be at least 1")
        top = limit if limit is not None else _DEFAULT_TOP_DENIED
        client = (request.query.get("client") or "").strip() or None
        rows = repo.aggregate_domain_totals(denied_only=True, limit=top, client=client)
        clients = repo.clients_for_domains(
            [row["domain"] for row in rows], denied_only=True, client=client
        )
        for row in rows:
            row["clients"] = clients.get(row["domain"], [])
        return {"status": "ok", "domains": rows, "total": len(rows)}

    @app.delete("/queries")
    def reset_queries() -> dict[str, Any]:
        deleted = repo.reset_queries()
        return {"status": "ok", "deleted": deleted}
