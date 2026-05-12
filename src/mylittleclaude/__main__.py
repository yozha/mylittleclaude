from __future__ import annotations

import argparse
import asyncio
import logging
import sys

from .config import ConfigError, check_config_cli, load_config
from .log import setup_logging


def main() -> int:
    parser = argparse.ArgumentParser(prog="mylittleclaude")
    parser.add_argument(
        "--check-config", action="store_true",
        help="Validate .env and servers.yaml, then exit 0/1.",
    )
    args = parser.parse_args()

    setup_logging()

    if args.check_config:
        return check_config_cli()

    try:
        cfg = load_config()
    except ConfigError as e:
        logging.getLogger(__name__).error("config error: %s", e)
        return 1

    # Defer importing bot until config is loaded — keeps --check-config fast.
    from .bot import run as run_bot
    try:
        asyncio.run(run_bot(cfg))
    except KeyboardInterrupt:
        logging.getLogger(__name__).info("shutdown requested")
        return 0
    return 0


if __name__ == "__main__":
    sys.exit(main())
