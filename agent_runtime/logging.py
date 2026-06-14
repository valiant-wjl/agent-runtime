"""RotatingFileHandler setup for runtime file logging."""

import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path


def setup_file_logging(
    log_file: Path,
    max_bytes: int = 10 * 1024 * 1024,
    backups: int = 5,
) -> None:
    """Attach a RotatingFileHandler to the root logger.

    Call from main() once logging.basicConfig has been done.
    This ADDS a file handler — stderr output is unchanged.
    Idempotent: calling again with the same path is a no-op.
    """
    log_file = Path(log_file).resolve()
    root = logging.getLogger()
    # Prevent duplicate handlers (e.g. accidental double-call in tests / reload)
    for h in root.handlers:
        if isinstance(h, RotatingFileHandler) and Path(h.baseFilename).resolve() == log_file:
            return
    log_file.parent.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(
        log_file,
        maxBytes=max_bytes,
        backupCount=backups,
        encoding="utf-8",
    )
    handler.setFormatter(logging.Formatter(
        "%(asctime)s %(levelname)s %(name)s: %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    ))
    handler.setLevel(logging.INFO)
    logging.getLogger().addHandler(handler)
