from typing import Any

from bottle import Bottle

from mirenai.api.errors import error, parse_body
from mirenai.api.schemas import SettingsUpdate
from mirenai.repository import settings as repo


def register_settings_routes(app: Bottle) -> None:
    @app.get("/settings")
    def get_settings() -> dict[str, Any]:
        return {"status": "ok", "settings": repo.get_all_settings()}

    @app.put("/settings")
    def update_settings() -> dict[str, Any]:
        model, err = parse_body(SettingsUpdate)
        if model is None:
            return err or error("bad_request", "Invalid request", 400)
        try:
            updated = repo.update_settings(model.model_dump(exclude_unset=True))
        except ValueError as exc:
            return error("validation_error", "Invalid request", 422, str(exc))
        return {"status": "ok", "settings": updated}
