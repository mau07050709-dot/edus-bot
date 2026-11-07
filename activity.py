"""Registro local con rotación y hora de Costa Rica."""

import logging
from datetime import datetime
from logging.handlers import RotatingFileHandler
from zoneinfo import ZoneInfo


COSTA_RICA = ZoneInfo("America/Costa_Rica")


def costa_rica_now():
    return datetime.now(COSTA_RICA)


class LocalTimeFormatter(logging.Formatter):
    def formatTime(self, record, datefmt=None):
        return datetime.fromtimestamp(record.created, COSTA_RICA).strftime(
            "%Y-%m-%d %H:%M:%S %z"
        )


def create_logger(directory):
    logger = logging.Logger("edus_bot")
    logger.setLevel(logging.INFO)
    directory.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        directory / "activity.log", maxBytes=512_000, backupCount=2, encoding="utf-8"
    )
    handler.setFormatter(LocalTimeFormatter("%(asctime)s %(levelname)s %(message)s"))
    logger.addHandler(handler)
    return logger
