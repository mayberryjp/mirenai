"""Background notification persistence and delivery within the DNS process."""

from __future__ import annotations

import threading
import time

from mirenai.domain.notifications import NotificationBuffer
from mirenai.logging import get_logger
from mirenai.repository.notifications import deliver_pending

log = get_logger("discord.worker")


class NotificationWorker:
    def __init__(self, buffer: NotificationBuffer) -> None:
        self._buffer = buffer
        self._stop = threading.Event()
        self._thread = threading.Thread(
            target=self._run, name="discord-notifications", daemon=True
        )

    def start(self) -> None:
        self._thread.start()

    def _run(self) -> None:
        next_delivery = 0.0
        while not self._stop.wait(1):
            self._buffer.flush()
            if time.monotonic() < next_delivery:
                continue
            try:
                delay = deliver_pending()
                next_delivery = time.monotonic() + delay
            except Exception:
                log.exception("Discord notification processing failed")
                next_delivery = time.monotonic() + 5
        self._buffer.flush()

    def stop(self) -> None:
        self._stop.set()
        self._thread.join()
