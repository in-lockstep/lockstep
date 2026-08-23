# Extracted from pipeline-framework src/utils/logger.py on 2026-08-23.
# Behaviour is preserved verbatim; only imports and configuration plumbing were adapted.
from __future__ import annotations

import logging
import sys


def setup_logger(level: str = "info") -> logging.Logger:
    """Configure and return the pipeline logger."""
    logger = logging.getLogger("pipeline")
    logger.setLevel(getattr(logging, level.upper(), logging.INFO))

    if not logger.handlers:
        handler = logging.StreamHandler(sys.stderr)
        handler.setFormatter(logging.Formatter("%(message)s"))
        logger.addHandler(handler)

    return logger


log = setup_logger()
