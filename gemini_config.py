"""Single source of truth for Gemini model selection.

Why this module exists
----------------------
Every module that talks to Gemini used to carry its own hard-coded default,
and they had drifted apart: the daily pipeline (``ingest_engine``) ran on
``gemini-3.5-flash-lite`` while ``telegram_bot`` / ``cross_source_verifier`` /
``auto_numeric_extractor`` were still pinned to the legacy ``gemini-2.5-flash``
(Google's documented replacement for it is Gemini 3.5 Flash-Lite).  Only the
ingest engine had a fallback chain, although the primary model does get
rate-limited (HTTP 429) during production runs.

Configuration (all optional — blank values count as unset)
----------------------------------------------------------
``GEMINI_MODEL``            primary model.
                            default: ``gemini-3.5-flash-lite``
``GEMINI_FALLBACK_MODELS``  comma-separated fallbacks, tried in order.
                            default: ``gemini-3.6-flash,gemini-flash-latest``

The effective chain is :data:`GEMINI_MODELS` (primary first, de-duplicated).
Use :func:`generate_content_with_fallback` instead of calling
``client.models.generate_content`` directly so every caller gets the same
fallback behaviour.
"""

import logging
import os
from typing import Callable, Iterable, List, Mapping, Optional, Tuple

logger = logging.getLogger("gemini_config")

# Stable Gemini 3 generation models (see https://ai.google.dev/gemini-api/docs/models).
# Keep 1.x / 2.x names out of here — they are the legacy generation and were
# the reason the defaults had to be fixed in the first place.
DEFAULT_GEMINI_MODEL = "gemini-3.5-flash-lite"
DEFAULT_FALLBACK_MODELS: Tuple[str, ...] = ("gemini-3.6-flash", "gemini-flash-latest")

PRIMARY_ENV_VAR = "GEMINI_MODEL"
FALLBACK_ENV_VAR = "GEMINI_FALLBACK_MODELS"


def _split_csv(value: Optional[str]) -> List[str]:
    """``"a, b,,a "`` -> ``["a", "b"]`` (trimmed, empties dropped, order kept)."""
    if not value:
        return []
    return list(dict.fromkeys(item.strip() for item in value.split(",") if item.strip()))


def load_from_env(environ: Optional[Mapping[str, str]] = None) -> Tuple[str, List[str]]:
    """Read ``(primary, fallbacks)`` from *environ* (default ``os.environ``).

    Blank / whitespace-only values fall back to the defaults, so a workflow can
    forward ``GEMINI_MODEL: ${{ secrets.GEMINI_MODEL }}`` without guarding it.
    """
    env = os.environ if environ is None else environ
    primary = (env.get(PRIMARY_ENV_VAR) or "").strip() or DEFAULT_GEMINI_MODEL
    fallbacks = _split_csv(env.get(FALLBACK_ENV_VAR)) or list(DEFAULT_FALLBACK_MODELS)
    return primary, fallbacks


def resolve_models(primary: Optional[str] = None, fallbacks: Optional[Iterable[str]] = None) -> List[str]:
    """Ordered, de-duplicated model chain: *primary* first, then *fallbacks*.

    ``None`` for either argument means "use the configured value".
    """
    head = (primary or "").strip() or GEMINI_MODEL
    tail = list(GEMINI_FALLBACK_MODELS) if fallbacks is None else [m.strip() for m in fallbacks if m and m.strip()]
    return list(dict.fromkeys([head, *tail]))


GEMINI_MODEL, GEMINI_FALLBACK_MODELS = load_from_env()
GEMINI_MODELS: List[str] = resolve_models(GEMINI_MODEL, GEMINI_FALLBACK_MODELS)


def describe_models(models: Optional[Iterable[str]] = None) -> str:
    """Human-readable chain for logs / workflow summaries."""
    return " -> ".join(GEMINI_MODELS if models is None else models)


def _short(error: BaseException, limit: int = 200) -> str:
    text = str(error).replace("\n", " ").strip()
    return text if len(text) <= limit else text[: limit - 3] + "..."


def generate_content_with_fallback(
    client,
    contents,
    config=None,
    models: Optional[Iterable[str]] = None,
    parse: Optional[Callable[[str], object]] = None,
):
    """Call ``client.models.generate_content`` trying each model in turn.

    The next model is tried when the current one raises (429 RESOURCE_EXHAUSTED,
    404 for a retired model name, 503 UNAVAILABLE, ...), returns no text, or
    when *parse* rejects the text (e.g. ``json.loads`` on a truncated answer).

    Returns ``parse(text)`` when *parse* is given, otherwise the raw response.
    Raises the last error once every model in the chain has failed, so callers
    keep their existing ``except`` handling.
    """
    chain = list(models) if models is not None else list(GEMINI_MODELS)
    if not chain:
        raise RuntimeError("No Gemini models configured (GEMINI_MODEL / GEMINI_FALLBACK_MODELS).")

    last_error: Optional[BaseException] = None
    for index, model_name in enumerate(chain):
        try:
            response = client.models.generate_content(model=model_name, contents=contents, config=config)
            text = getattr(response, "text", None)
            if not text or not text.strip():
                raise ValueError("Gemini returned an empty response")
            return parse(text) if parse is not None else response
        except Exception as error:  # noqa: BLE001 - any failure moves to the next model
            last_error = error
            next_model = chain[index + 1] if index + 1 < len(chain) else None
            if next_model:
                logger.warning(
                    "Gemini model %s failed (%s: %s); falling back to %s",
                    model_name, type(error).__name__, _short(error), next_model,
                )
            else:
                logger.warning(
                    "Gemini model %s failed (%s: %s); no fallback model left",
                    model_name, type(error).__name__, _short(error),
                )

    assert last_error is not None
    raise last_error
