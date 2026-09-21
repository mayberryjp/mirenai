from bottle import Bottle, response

from mirenai.db import check_database

SERVICE_NAME = "mirenai-api"


def register_health_routes(app: Bottle) -> None:
    @app.get("/health")
    def health() -> dict[str, str]:
        return {"status": "ok", "service": SERVICE_NAME}

    @app.get("/ready")
    def ready() -> dict[str, str]:
        ok, detail = check_database()
        if not ok:
            response.status = 503
            return {"status": "error", "code": "not_ready", "error": detail}
        return {"status": "ok"}
