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


def log_upstream(*, account_id: str, service: str, endpoint: str, status: int, error: str) -> None:
    """One triage line. No codes, tokens, secrets, or URLs."""
    account = re.sub(r"[^A-Za-z0-9_-]", "", str(account_id))[:64]
    service_name = re.sub(r"[^a-z]", "", str(service))[:16]
    endpoint_name = re.sub(r"[^a-z.]", "", str(endpoint))[:32]
    error_name = re.sub(r"[^A-Za-z0-9_-]", "", str(error or ""))[:40] or "none"
    sys.stderr.write(
        "ssios.ui connector"
        f" account={account}"
        f" service={service_name}"
        f" endpoint={endpoint_name}"
        f" status={int(status)}"
        f" error={error_name}\n"
    )
    sys.stderr.flush()
