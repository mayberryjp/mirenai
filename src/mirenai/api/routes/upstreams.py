from typing import Any

from bottle import Bottle, response

from mirenai.api.errors import error, parse_body, read_pagination
from mirenai.api.schemas import UpstreamCreate, UpstreamUpdate
from mirenai.repository import upstreams as repo


def register_upstream_routes(app: Bottle) -> None:
    @app.get("/upstreams")
    def list_upstreams() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        if not paginate:
            rows = repo.list_upstreams()
            return {"status": "ok", "upstreams": rows, "total": len(rows)}
        rows = repo.list_upstreams(limit=limit, offset=offset)
        return {"status": "ok", "upstreams": rows, "total": repo.count_upstreams()}

    @app.get("/upstreams/<upstream_id:int>")
    def get_upstream(upstream_id: int) -> dict[str, Any]:
        row = repo.get_upstream(upstream_id)
        if row is None:
            return error("not_found", "not found", 404)
        return {"status": "ok", "upstream": row}

    @app.post("/upstreams")
    def create_upstream() -> dict[str, Any]:
        model, err = parse_body(UpstreamCreate)
        if model is None:
            return err or error("bad_request", "Invalid request", 400)
        row = repo.create_upstream(model.model_dump())
        response.status = 201
        return {"status": "ok", "upstream": row}

    @app.put("/upstreams/<upstream_id:int>")
    def update_upstream(upstream_id: int) -> dict[str, Any]:
        model, err = parse_body(UpstreamUpdate)
        if model is None:
            return err or error("bad_request", "Invalid request", 400)
        row = repo.update_upstream(upstream_id, model.model_dump(exclude_unset=True))
        if row is None:
            return error("not_found", "not found", 404)
        return {"status": "ok", "upstream": row}

    @app.delete("/upstreams/<upstream_id:int>")
    def delete_upstream(upstream_id: int) -> dict[str, Any]:
        if not repo.delete_upstream(upstream_id):
            return error("not_found", "not found", 404)
        return {"status": "ok", "deleted": upstream_id}
