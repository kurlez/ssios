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


_SCOPE_URL = re.compile(r"https://www\.googleapis\.com/auth/[A-Za-z0-9._-]+")


def _scope_names(text: str) -> str:
    found = _SCOPE_URL.findall(text or "")
    return ",".join(found) if found else "none"


def log_token_scopes(*, account_id: str, service: str, requested: str, granted: str) -> None:
    """One line of scope URLs after a token exchange. No token and no other URL."""
    account = re.sub(r"[^A-Za-z0-9_-]", "", str(account_id))[:64]
    service_name = re.sub(r"[^a-z]", "", str(service))[:16]
    sys.stderr.write(
        "ssios.ui connector"
        f" account={account}"
        f" service={service_name}"
        " endpoint=token status=200"
        f" granted={_scope_names(granted)}"
        f" requested={_scope_names(requested)}\n"
    )
    sys.stderr.flush()


def log_read(*, account_id: str, service: str, endpoint: str, status: int, query: str, count: int) -> None:
    """One line for a Gmail or Drive read. The query is operators only, never a subject or a name."""
    account = re.sub(r"[^A-Za-z0-9_-]", "", str(account_id))[:64]
    service_name = re.sub(r"[^a-z]", "", str(service))[:16]
    endpoint_name = re.sub(r"[^a-z.]", "", str(endpoint))[:32]
    shape = re.sub(r"[^A-Za-z0-9:_,.+-]", "", str(query))[:80] or "none"
    sys.stderr.write(
        "ssios.ui connector"
        f" account={account}"
        f" service={service_name}"
        f" endpoint={endpoint_name}"
        f" status={int(status)}"
        f" query={shape}"
        f" count={max(0, int(count))}\n"
    )
    sys.stderr.flush()


def gmail_query_shape(query: str) -> str:
    """Keep operator names. Keep a value only when it is a fixed token such as inbox or 7d."""
    safe_value = re.compile(r"^[A-Za-z0-9_@./+-]{1,32}$")
    safe_ops = {"in", "label", "is", "newer_than", "older_than", "after", "before", "has", "category"}
    kept = []
    for token in str(query or "").replace("(", " ").replace(")", " ").split():
        if ":" not in token:
            continue
        key, _, value = token.partition(":")
        key = re.sub(r"[^a-z]", "", key.lower())
        if not key:
            continue
        if key in safe_ops and safe_value.fullmatch(value):
            kept.append(f"{key}:{value}")
        else:
            kept.append(key)
    return ",".join(kept) or "in:inbox"


def log_upstream(*, account_id: str, service: str, endpoint: str, status: int, error: str, reason: str = "") -> None:
    """One triage line. No codes, tokens, secrets, message bodies, or URLs.

    ``error`` is the OAuth error or the Google status. ``reason`` is the
    ErrorInfo reason field, such as SERVICE_DISABLED or accessNotConfigured.
    """
    account = re.sub(r"[^A-Za-z0-9_-]", "", str(account_id))[:64]
    service_name = re.sub(r"[^a-z]", "", str(service))[:16]
    endpoint_name = re.sub(r"[^a-z.]", "", str(endpoint))[:32]
    error_name = re.sub(r"[^A-Za-z0-9_-]", "", str(error or ""))[:40] or "none"
    reason_name = re.sub(r"[^A-Za-z0-9_-]", "", str(reason or ""))[:48] or "none"
    sys.stderr.write(
        "ssios.ui connector"
        f" account={account}"
        f" service={service_name}"
        f" endpoint={endpoint_name}"
        f" status={int(status)}"
        f" error={error_name}"
        f" reason={reason_name}\n"
    )
    sys.stderr.flush()
