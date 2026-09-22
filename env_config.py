"""Validated environment-variable readers.

Why this module exists
----------------------
Every module used to parse its numeric settings inline, e.g.::

    SEARCH_WINDOW_DAYS = int(os.getenv("SEARCH_WINDOW_DAYS", 14))
    MOI_MAX_WORKERS    = int(os.environ.get("MOI_MAX_WORKERS", "8"))

Those run at *import* time, so one malformed value — ``"14 days"``, ``"eight"``,
an empty string forwarded from CI — raised an opaque ``ValueError`` that took
down the whole module (and, because the test suite asserts clean imports, the
entire application).

The helpers below never raise. A bad value is logged once as a warning and the
documented default is used instead, so a typo in a workflow secret degrades
into a visible log line rather than a dead process.
"""

import logging
import os
from typing import Mapping, Optional

logger = logging.getLogger("env_config")


def _warn(name: str, raw: object, default: object, expected: str) -> None:
    logger.warning(
        "Invalid %s=%r (expected %s); using default %r", name, raw, expected, default
    )


def get_int(
    name: str,
    default: int,
    *,
    minimum: Optional[int] = None,
    maximum: Optional[int] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> int:
    """Read an int from the environment, falling back to *default* on any problem.

    Optional ``minimum``/``maximum`` clamp out-of-range values (still logging),
    which protects against e.g. ``MOI_MAX_WORKERS=0`` deadlocking a pool.
    """
    env = os.environ if environ is None else environ
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default

    try:
        value = int(str(raw).strip())
    except (TypeError, ValueError):
        _warn(name, raw, default, "an integer")
        return default

    if minimum is not None and value < minimum:
        _warn(name, raw, default, f"an integer >= {minimum}")
        return default
    if maximum is not None and value > maximum:
        _warn(name, raw, default, f"an integer <= {maximum}")
        return default
    return value


def get_float(
    name: str,
    default: float,
    *,
    minimum: Optional[float] = None,
    maximum: Optional[float] = None,
    environ: Optional[Mapping[str, str]] = None,
) -> float:
    """Float counterpart of :func:`get_int`."""
    env = os.environ if environ is None else environ
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default

    try:
        value = float(str(raw).strip())
    except (TypeError, ValueError):
        _warn(name, raw, default, "a number")
        return default

    if minimum is not None and value < minimum:
        _warn(name, raw, default, f"a number >= {minimum}")
        return default
    if maximum is not None and value > maximum:
        _warn(name, raw, default, f"a number <= {maximum}")
        return default
    return value


def get_bool(name: str, default: bool = False, *, environ: Optional[Mapping[str, str]] = None) -> bool:
    """Truthy parser for ``"1"``/``"true"``/``"yes"``/``"on"`` (case-insensitive)."""
    env = os.environ if environ is None else environ
    raw = env.get(name)
    if raw is None or not str(raw).strip():
        return default
    return str(raw).strip().lower() in {"1", "true", "yes", "on"}


def get_str(name: str, default: str = "", *, environ: Optional[Mapping[str, str]] = None) -> str:
    """Non-empty string, trimmed. Whitespace-only counts as unset."""
    env = os.environ if environ is None else environ
    raw = env.get(name)
    if raw is None:
        return default
    value = str(raw).strip()
    return value or default


# ------------------------------------------------------------
# Shared credential resolution
# ------------------------------------------------------------
# The Supabase key fallback chain used to be copy-pasted into five modules and
# the Gemini key chain into two. They are defined once here so they cannot
# drift apart (the same failure mode the gemini_config model work addressed).

SUPABASE_KEY_VARS = ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY", "SUPABASE_ANON_KEY")
GEMINI_KEY_VARS = ("GEMINI_API_KEY", "GOOGLE_API_KEY")


def _first_set(names, environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    env = os.environ if environ is None else environ
    for name in names:
        value = (env.get(name) or "").strip()
        if value:
            return value
    return None


def get_supabase_credentials(
    environ: Optional[Mapping[str, str]] = None,
) -> "tuple[Optional[str], Optional[str]]":
    """Return ``(url, key)`` for Supabase, or ``(None, None)`` if unconfigured.

    *key* falls back through :data:`SUPABASE_KEY_VARS` so a deployment that only
    defines ``SUPABASE_ANON_KEY`` still works.
    """
    env = os.environ if environ is None else environ
    url = (env.get("SUPABASE_URL") or "").strip() or None
    return url, _first_set(SUPABASE_KEY_VARS, env)


def get_gemini_api_key(environ: Optional[Mapping[str, str]] = None) -> Optional[str]:
    """Return the Gemini API key from either accepted variable name."""
    return _first_set(GEMINI_KEY_VARS, environ)
