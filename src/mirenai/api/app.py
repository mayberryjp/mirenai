import json
import time
from typing import Any

from bottle import Bottle, request, response

from mirenai.api.routes.blocklists import register_blocklist_routes
from mirenai.api.routes.health import register_health_routes
from mirenai.api.routes.hosts import register_host_routes
from mirenai.api.routes.policies import register_policy_routes
from mirenai.api.routes.query_log import register_query_routes
from mirenai.api.routes.requests import register_request_routes
from mirenai.api.routes.settings import register_settings_routes
from mirenai.api.routes.stats import register_stats_routes
from mirenai.api.routes.upstreams import register_upstream_routes
from mirenai.logging import get_logger

SERVICE_NAME = "mirenai-api"
_START_KEY = "mirenai.request_start"

log = get_logger("api.request")


def create_app() -> Bottle:
    app = Bottle()
    app.title = SERVICE_NAME

    register_health_routes(app)
    register_policy_routes(app)
    register_upstream_routes(app)
    register_query_routes(app)
    register_settings_routes(app)
    register_blocklist_routes(app)
    register_host_routes(app)
    register_stats_routes(app)
    register_request_routes(app)

    @app.hook("before_request")
    def start_timer() -> None:
        request.environ[_START_KEY] = time.monotonic()

    @app.hook("after_request")
    def finalize() -> None:
        response.headers["Access-Control-Allow-Origin"] = "*"
        response.headers["Access-Control-Allow-Methods"] = "GET, POST, PUT, PATCH, DELETE, OPTIONS"
        response.headers["Access-Control-Allow-Headers"] = "*"
        start = request.environ.get(_START_KEY)
        if start is not None:
            duration_ms = (time.monotonic() - start) * 1000
            log.info(
                "method=%s path=%s status=%s duration_ms=%.1f",
                request.method,
                request.path,
                response.status_code,
                duration_ms,
            )

    @app.route("/<path:path>", method="OPTIONS")
    def cors_preflight(path: str) -> str:
        return ""

    @app.error(404)
    def not_found(_err: Any) -> str:
        response.content_type = "application/json"
        return json.dumps({"status": "error", "code": "not_found", "error": "not found"})

    @app.error(500)
    def server_error(_err: Any) -> str:
        response.content_type = "application/json"
        return json.dumps({"status": "error", "code": "internal_error", "error": "internal error"})

    return app


app = create_app()
