# src/facebook_marketplace_scraper/logging_config.py
from __future__ import annotations

import atexit
import json
import logging
import queue
from datetime import UTC, datetime
from logging.handlers import QueueHandler, QueueListener

_listener: QueueListener | None = None


class JsonFormatter(logging.Formatter):
    def format(self, record: logging.LogRecord) -> str:
        payload: dict[str, object] = {
            "timestamp": datetime.now(UTC).isoformat(),
            "level": record.levelname,
            "logger": record.name,
            "message": record.getMessage(),
        }
        for key in ("watchlist", "listing_id", "run_id", "event", "duration_ms"):
            if hasattr(record, key):
                payload[key] = getattr(record, key)
        if record.exc_info:
            payload["exception"] = self.formatException(record.exc_info)
        return json.dumps(payload, ensure_ascii=False, separators=(",", ":"))


def configure_logging(*, level: str = "INFO", fmt: str = "text") -> None:
    global _listener
    if _listener is not None:
        _listener.stop()

    sink = logging.StreamHandler()
    if fmt.casefold() == "json":
        sink.setFormatter(JsonFormatter())
    else:
        sink.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    log_queue: queue.SimpleQueue[logging.LogRecord] = queue.SimpleQueue()
    queue_handler = QueueHandler(log_queue)
    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(queue_handler)
    root.setLevel(getattr(logging, level.upper(), logging.INFO))

    _listener = QueueListener(log_queue, sink, respect_handler_level=True)
    _listener.start()


def shutdown_logging() -> None:
    global _listener
    if _listener is not None:
        _listener.stop()
        _listener = None


atexit.register(shutdown_logging)
