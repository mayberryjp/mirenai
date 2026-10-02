from typing import Any

from bottle import Bottle, response
from sqlalchemy.exc import IntegrityError

from mirenai.api.errors import error, parse_body, read_pagination
from mirenai.api.schemas import TrustedNetworkCreate
from mirenai.repository import trusted_networks as repo


def register_trusted_network_routes(app: Bottle) -> None:
    @app.get("/trusted-networks")
    def list_trusted_networks() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        if not paginate:
            rows = repo.list_trusted_networks()
            return {"status": "ok", "trusted_networks": rows, "total": len(rows)}
        rows = repo.list_trusted_networks(limit=limit, offset=offset)
        return {"status": "ok", "trusted_networks": rows, "total": repo.count_trusted_networks()}

    @app.get("/trusted-networks/<network_id:int>")
    def get_trusted_network(network_id: int) -> dict[str, Any]:
        row = repo.get_trusted_network(network_id)
        if row is None:
            return error("not_found", "not found", 404)
        return {"status": "ok", "trusted_network": row}

    @app.post("/trusted-networks")
    def create_trusted_network() -> dict[str, Any]:
        model, err = parse_body(TrustedNetworkCreate)
        if model is None:
            return err or error("bad_request", "Invalid request", 400)
        try:
            row = repo.create_trusted_network(model.model_dump())
        except IntegrityError:
            return error("conflict", "this subnet is already trusted", 409)
        response.status = 201
        return {"status": "ok", "trusted_network": row}

    @app.delete("/trusted-networks/<network_id:int>")
    def delete_trusted_network(network_id: int) -> dict[str, Any]:
        if not repo.delete_trusted_network(network_id):
            return error("not_found", "not found", 404)
        return {"status": "ok", "deleted": network_id}
