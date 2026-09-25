from typing import Any

from bottle import Bottle, response
from sqlalchemy.exc import IntegrityError

from mirenai.api.errors import error, parse_body, read_pagination
from mirenai.api.schemas import BlocklistCreate, BlocklistUpdate
from mirenai.repository import blocklists as repo
from mirenai.workers.blocklist_downloader import BlocklistDownloadError, refresh_blocklist


def register_blocklist_routes(app: Bottle) -> None:
    @app.get("/blocklists")
    def list_blocklists() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        if not paginate:
            rows = repo.list_blocklists()
            return {"status": "ok", "blocklists": rows, "total": len(rows)}
        rows = repo.list_blocklists(limit=limit, offset=offset)
        return {"status": "ok", "blocklists": rows, "total": repo.count_blocklists()}

    @app.get("/blocklists/<blocklist_id:int>")
    def get_blocklist(blocklist_id: int) -> dict[str, Any]:
        row = repo.get_blocklist(blocklist_id)
        if row is None:
            return error("not_found", "not found", 404)
        return {"status": "ok", "blocklist": row}

    @app.post("/blocklists")
    def create_blocklist() -> dict[str, Any]:
        model, err = parse_body(BlocklistCreate)
        if model is None:
            return err or error("bad_request", "Invalid request", 400)
        try:
            row = repo.create_blocklist(model.model_dump())
        except IntegrityError:
            return error("conflict", "a blocklist with this name already exists", 409)
        response.status = 201
        return {"status": "ok", "blocklist": row}

    @app.put("/blocklists/<blocklist_id:int>")
    def update_blocklist(blocklist_id: int) -> dict[str, Any]:
        model, err = parse_body(BlocklistUpdate)
        if model is None:
            return err or error("bad_request", "Invalid request", 400)
        try:
            row = repo.update_blocklist(blocklist_id, model.model_dump(exclude_unset=True))
        except IntegrityError:
            return error("conflict", "a blocklist with this name already exists", 409)
        if row is None:
            return error("not_found", "not found", 404)
        return {"status": "ok", "blocklist": row}

    @app.delete("/blocklists/<blocklist_id:int>")
    def delete_blocklist(blocklist_id: int) -> dict[str, Any]:
        if not repo.delete_blocklist(blocklist_id):
            return error("not_found", "not found", 404)
        return {"status": "ok", "deleted": blocklist_id}

    @app.get("/blocklists/<blocklist_id:int>/domains")
    def list_blocklist_domains(blocklist_id: int) -> dict[str, Any]:
        if repo.get_blocklist(blocklist_id) is None:
            return error("not_found", "not found", 404)
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        if not paginate:
            domains = repo.list_domains(blocklist_id)
            return {"status": "ok", "domains": domains, "total": len(domains)}
        domains = repo.list_domains(blocklist_id, limit=limit, offset=offset)
        return {"status": "ok", "domains": domains, "total": repo.count_domains(blocklist_id)}

    @app.post("/blocklists/<blocklist_id:int>/refresh")
    def refresh_blocklist_now(blocklist_id: int) -> dict[str, Any]:
        if repo.get_blocklist(blocklist_id) is None:
            return error("not_found", "not found", 404)
        try:
            row = refresh_blocklist(blocklist_id)
        except BlocklistDownloadError as exc:
            return error("download_failed", "blocklist download failed", 502, str(exc))
        return {"status": "ok", "blocklist": row}
