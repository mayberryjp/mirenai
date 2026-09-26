from ipaddress import ip_address
from typing import Any

from bottle import Bottle

from mirenai.api.errors import error, parse_body
from mirenai.api.schemas import ClientModeUpdate
from mirenai.repository import policies as repo


def register_client_mode_routes(app: Bottle) -> None:
    @app.get("/clients/<ip>/mode")
    def get_mode(ip: str) -> dict[str, Any]:
        err = _validate_ip(ip)
        if err is not None:
            return err
        return {"status": "ok", **repo.get_client_mode(ip)}

    @app.put("/clients/<ip>/mode")
    def set_mode(ip: str) -> dict[str, Any]:
        err = _validate_ip(ip)
        if err is not None:
            return err
        model, body_err = parse_body(ClientModeUpdate)
        if model is None:
            return body_err or error("bad_request", "Invalid request", 400)
        return {"status": "ok", **repo.set_client_mode(ip, model.model_dump()["mode"])}


def _validate_ip(ip: str) -> dict[str, Any] | None:
    try:
        ip_address(ip)
    except ValueError:
        return error("validation_error", "Invalid request", 422, "client must be a valid IP address")
    return None
