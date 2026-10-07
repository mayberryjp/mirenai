from typing import Any

from bottle import Bottle

from mirenai.api.errors import error, read_pagination
from mirenai.repository import foreign_clients as repo


def register_foreign_client_routes(app: Bottle) -> None:
    @app.get("/foreign-clients")
    def list_foreign_clients() -> dict[str, Any]:
        paginate, limit, offset, err = read_pagination()
        if err is not None:
            return err
        if not paginate:
            rows = repo.list_foreign_clients()
            return {"status": "ok", "foreign_clients": rows, "total": len(rows)}
        rows = repo.list_foreign_clients(limit=limit, offset=offset)
        return {"status": "ok", "foreign_clients": rows, "total": repo.count_foreign_clients()}

    @app.delete("/foreign-clients")
    def clear_foreign_clients() -> dict[str, Any]:
        return {"status": "ok", "deleted": repo.delete_all_foreign_clients()}

    @app.delete("/foreign-clients/<client_id:int>")
    def delete_foreign_client(client_id: int) -> dict[str, Any]:
        if not repo.delete_foreign_client(client_id):
            return error("not_found", "not found", 404)
        return {"status": "ok", "deleted": client_id}
