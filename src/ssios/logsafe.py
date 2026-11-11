"""UI log lines. Tracebacks stay, secret values do not."""

from __future__ import annotations

import re
import sys
import traceback

_SECRET = re.compile(
    r"(?i)(ya29\.[0-9A-Za-z._-]+|GOCSPX-[0-9A-Za-z._-]+|"
    r"(?:refresh_token|client_secret|access_token|password)\s*[:=]\s*\S+|"
    r"Bearer\s+\S+|ssios_session=\S+)"
)

INTERNAL_HINT = (
    "Something went wrong. Error code: internal. "
    "Hint: run ./scripts/ssios logs. The log has the error class and a traceback, not the client secret."
)


def redact(text: str) -> str:
    return _SECRET.sub("[redacted]", text)


def log_failure(exc: BaseException) -> None:
    """Write the exception class and traceback to stderr, which start saves as the UI log."""
    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    sys.stderr.write(f"ssios.ui error class={exc.__class__.__name__}\n{redact(rendered)}")
    sys.stderr.flush()
