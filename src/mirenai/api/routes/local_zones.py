from typing import Any

from bottle import Bottle, request, response
from sqlalchemy.exc import IntegrityError

from mirenai.api.errors import error, parse_body, read_pagination
from mirenai.api.schemas import LocalZoneCreate, LocalZoneUpdate
from mirenai.repository import local_zones as repo
from mirenai.workers.local_zones import LocalZoneDownloadError, refresh_local_zone


def register_local_zone_routes(app: Bottle) -> None:
    @app.get("/local-zones")
    def list_local_zones() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        if not paginate:
            rows = repo.list_local_zones()
            return {"status": "ok", "local_zones": rows, "total": len(rows)}
        rows = repo.list_local_zones(limit=limit, offset=offset)
        return {"status": "ok", "local_zones": rows, "total": repo.count_local_zones()}

    @app.get("/local-records")
    def list_local_records() -> dict[str, Any]:
        search = (request.query.get("search") or "").strip() or None
        rtype = (request.query.get("type") or "").strip() or None
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        if not paginate:
            rows = repo.list_all_records(search=search, rtype=rtype)
            return {"status": "ok", "records": rows, "total": len(rows)}
        rows = repo.list_all_records(limit=limit, offset=offset, search=search, rtype=rtype)
        total = repo.count_all_records(search=search, rtype=rtype)
        return {"status": "ok", "records": rows, "total": total}

    @app.get("/local-zones/<zone_id:int>")
    def get_local_zone(zone_id: int) -> dict[str, Any]:
        row = repo.get_local_zone(zone_id)
        if row is None:
            return error("not_found", "not found", 404)
        return {"status": "ok", "local_zone": row}

    @app.post("/local-zones")
    def create_local_zone() -> dict[str, Any]:
        model, err = parse_body(LocalZoneCreate)
        if model is None:
            return err or error("bad_request", "Invalid request", 400)
        try:
            row = repo.create_local_zone(model.model_dump())
        except IntegrityError:
            return error("conflict", "a local zone with this name already exists", 409)
        response.status = 201
        return {"status": "ok", "local_zone": row}

    @app.put("/local-zones/<zone_id:int>")
    def update_local_zone(zone_id: int) -> dict[str, Any]:
        model, err = parse_body(LocalZoneUpdate)
        if model is None:
            return err or error("bad_request", "Invalid request", 400)
        try:
            row = repo.update_local_zone(zone_id, model.model_dump(exclude_unset=True))
        except IntegrityError:
            return error("conflict", "a local zone with this name already exists", 409)
        if row is None:
            return error("not_found", "not found", 404)
        return {"status": "ok", "local_zone": row}

    @app.delete("/local-zones/<zone_id:int>")
    def delete_local_zone(zone_id: int) -> dict[str, Any]:
        if not repo.delete_local_zone(zone_id):
            return error("not_found", "not found", 404)
        return {"status": "ok", "deleted": zone_id}

    @app.get("/local-zones/<zone_id:int>/records")
    def list_zone_records(zone_id: int) -> dict[str, Any]:
        if repo.get_local_zone(zone_id) is None:
            return error("not_found", "not found", 404)
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        if not paginate:
            records = repo.list_records(zone_id)
            return {"status": "ok", "records": records, "total": len(records)}
        records = repo.list_records(zone_id, limit=limit, offset=offset)
        return {"status": "ok", "records": records, "total": repo.count_records(zone_id)}

    @app.post("/local-zones/<zone_id:int>/refresh")
    def refresh_local_zone_now(zone_id: int) -> dict[str, Any]:
        if repo.get_local_zone(zone_id) is None:
            return error("not_found", "not found", 404)
        try:
            row = refresh_local_zone(zone_id)
        except LocalZoneDownloadError as exc:
            return error("download_failed", "local zone download failed", 502, str(exc))
        return {"status": "ok", "local_zone": row}
