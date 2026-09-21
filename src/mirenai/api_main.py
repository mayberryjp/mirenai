from waitress import serve

from mirenai.api.app import create_app
from mirenai.config import settings
from mirenai.logging import configure_logging, get_logger


def main() -> None:
    configure_logging(settings.log_level)
    log = get_logger("api")
    app = create_app()
    log.info("starting api on %s:%s", settings.api_listen_address, settings.api_port)
    serve(app, host=settings.api_listen_address, port=settings.api_port)


if __name__ == "__main__":
    main()
