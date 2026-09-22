"""Myanmar Intelligent Newsroom — MCP server.

Exposes four read-only tools over Supabase RPC so an LLM client can search the
newsroom, walk an event timeline, compare the two state papers and pull a daily
briefing.

Every tool returns a JSON *string*. Failures are returned as
``{"error": "..."}`` rather than prose, because the consumer is a language model:
a bare "Error searching news: ..." reads exactly like news content and will be
quoted back as if it were data.
"""

import json
import logging
import re
from datetime import datetime

# Must run before get_supabase_credentials() below. This was the only entry point
# in the project that did not load .env, so `python newsroom_mcp.py` on a machine
# configured through .env started the server happily and then failed every single
# tool call with "SUPABASE_URL ... မရှိပါ". main.py picks dotenv up transitively
# via ingest_engine; telegram_bot / cross_source_verifier / fetch_independent_news
# load it themselves. dotenv stays optional.
try:
    from dotenv import load_dotenv

    load_dotenv()
except ImportError:  # pragma: no cover - dotenv is an optional convenience
    pass

from env_config import get_supabase_credentials

logger = logging.getLogger("NewsroomMCP")

# Supabase Credentials — env only; a hardcoded fallback can silently point the
# MCP server at the wrong project.
SUPABASE_URL, SUPABASE_KEY = get_supabase_credentials()

DEFAULT_SEARCH_LIMIT = 5

# Nothing caps `p_limit` server-side: p_limit=1000000 returned the entire match
# set, which would be dumped straight into the model's context window.
MAX_SEARCH_LIMIT = 50

# The other three RPCs accept no limit at all and return every matching row with
# its full `body_text`. Measured against production, one call returned 442
# articles / 1150 KB / ~295,000 tokens — roughly 30% of a 1M-token window, and
# an outright overflow for a 128k model. The SQL functions live server-side, so
# the rows are capped here instead.
DEFAULT_LIST_LIMIT = 10
MAX_LIST_LIMIT = 50

_DATE_RE = re.compile(r"^\d{4}-\d{2}-\d{2}$")

_client = None


def get_supabase_client():
    """Lazy init so importing this module never raises on missing credentials.

    Cached: the client owns an HTTP connection pool, and building a fresh one per
    tool call leaked a pool per request on a long-running SSE server.
    """
    global _client
    if _client is None:
        if not SUPABASE_URL or not SUPABASE_KEY:
            raise RuntimeError(
                "❌ SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY မရှိပါ။ "
                "Environment variables ကို စစ်ဆေးပါ။"
            )
        from supabase import create_client

        _client = create_client(SUPABASE_URL, SUPABASE_KEY)
    return _client


def reset_supabase_client():
    """Drop the cached client (tests, or after credentials change)."""
    global _client
    _client = None


def _error(message: str) -> str:
    """Structured, non-leaky failure payload."""
    return json.dumps({"error": message}, ensure_ascii=False)


def _clamp_limit(limit, default=DEFAULT_SEARCH_LIMIT, maximum=MAX_SEARCH_LIMIT) -> int:
    """Coerce an LLM-supplied ``limit`` into a safe range.

    MCP arguments arrive as JSON, so this may be a string, a float, negative or
    enormous. Postgres rejects a negative LIMIT outright (2201W) and nothing caps
    it from above.
    """
    try:
        value = int(limit)
    except (TypeError, ValueError):
        return default
    return max(1, min(value, maximum))


def _clean_term(value) -> str:
    """Trim a keyword, rejecting blank input.

    A blank keyword is not a no-op — the RPCs treat it as "match everything".
    ``fn_get_event_timeline('')`` returned 1000 rows, i.e. the whole table.
    """
    return str(value or "").strip()


def _is_iso_date(value: str) -> bool:
    """True for a real ``YYYY-MM-DD`` date.

    ``p_date`` is a Postgres ``date``, so anything else raises 22007
    ("invalid input syntax for type date") from the database.
    """
    if not _DATE_RE.match(value or ""):
        return False
    try:
        datetime.strptime(value, "%Y-%m-%d")
    except ValueError:
        return False
    return True


def _call_rpc(function: str, params: dict, limit=None) -> str:
    """Run a read-only RPC and JSON-encode the rows.

    The exception text goes to the log, not to the model: raw PostgREST payloads
    carry schema and constraint details the model has no business quoting.
    """
    try:
        response = get_supabase_client().rpc(function, params).execute()
        rows = response.data
        if limit is not None and isinstance(rows, list):
            rows = rows[:limit]
        return json.dumps(rows, ensure_ascii=False)
    except Exception as error:
        logger.warning("%s failed: %s", function, error)
        return _error(f"{function} failed. See the server log for details.")


def create_server():
    """Build the MCP server. Imported lazily so `mcp`/`uvicorn` are optional."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("Myanmar Intelligent Newsroom")

    @mcp.tool()
    def search_news(query: str, limit: int = DEFAULT_SEARCH_LIMIT) -> str:
        """Search verified news articles from Supabase Newsroom DB by keyword."""
        term = _clean_term(query)
        if not term:
            return _error("query must not be blank")
        return _call_rpc(
            "fn_search_news", {"p_query": term, "p_limit": _clamp_limit(limit)}
        )

    @mcp.tool()
    def get_event_timeline(keyword: str, limit: int = DEFAULT_LIST_LIMIT) -> str:
        """Get chronological event timeline for any event or news storyline by keyword or Event ID. Returns at most `limit` articles (default 10)."""
        term = _clean_term(keyword)
        if not term:
            return _error("keyword must not be blank")
        return _call_rpc(
            "fn_get_event_timeline",
            {"p_keyword": term},
            limit=_clamp_limit(limit, DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT),
        )

    @mcp.tool()
    def compare_news_sources(keyword: str, limit: int = DEFAULT_LIST_LIMIT) -> str:
        """Compare reporting differences between Myanma Alinn and The Mirror for any topic. Returns at most `limit` articles (default 10)."""
        term = _clean_term(keyword)
        if not term:
            return _error("keyword must not be blank")
        return _call_rpc(
            "fn_compare_sources",
            {"p_keyword": term},
            limit=_clamp_limit(limit, DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT),
        )

    @mcp.tool()
    def get_daily_briefing(date_str: str, limit: int = DEFAULT_LIST_LIMIT) -> str:
        """Get top daily intelligence briefing articles for a specific date (YYYY-MM-DD). Returns at most `limit` articles (default 10)."""
        value = _clean_term(date_str)
        if not _is_iso_date(value):
            return _error("date_str must be a valid YYYY-MM-DD date")
        return _call_rpc(
            "fn_get_daily_briefing",
            {"p_date": value},
            limit=_clamp_limit(limit, DEFAULT_LIST_LIMIT, MAX_LIST_LIMIT),
        )

    return mcp


if __name__ == "__main__":
    # Run as SSE server for Gemini Spark
    from env_config import get_int, get_str

    # A malformed PORT (e.g. "8000/tcp") must not crash the server at startup.
    port = get_int("PORT", 8000, minimum=1, maximum=65535)
    # Default preserved; set MCP_HOST=127.0.0.1 to stop listening on every
    # interface, which is what "0.0.0.0" does.
    host = get_str("MCP_HOST", "0.0.0.0")
    create_server().run(transport="sse", host=host, port=port)
