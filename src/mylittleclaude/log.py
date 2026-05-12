import logging
import os
import sys

_FORMAT = "%(asctime)s %(levelname)s %(name)s %(message)s"


def setup_logging(level: str | None = None) -> None:
    level_str = (level or os.environ.get("LOG_LEVEL") or "INFO").upper()
    numeric = getattr(logging, level_str, logging.INFO)
    root = logging.getLogger()
    for h in list(root.handlers):
        root.removeHandler(h)
    handler = logging.StreamHandler(sys.stderr)
    handler.setFormatter(logging.Formatter(_FORMAT))
    root.addHandler(handler)
    root.setLevel(numeric)
    # Quiet down chatty deps.
    logging.getLogger("httpx").setLevel(logging.WARNING)
    logging.getLogger("telegram.ext").setLevel(max(numeric, logging.INFO))


def short_excerpt(s: str | None, n: int = 200) -> str:
    if s is None:
        return ""
    if len(s) <= n:
        return s
    return s[:n] + "…"
