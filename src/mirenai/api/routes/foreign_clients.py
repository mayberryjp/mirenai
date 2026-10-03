from typing import Any

from bottle import Bottle

from mirenai.api.errors import read_pagination
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
