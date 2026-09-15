import os
import json

# Supabase Credentials — env only; a hardcoded fallback can silently point the
# MCP server at the wrong project.
SUPABASE_URL = os.environ.get("SUPABASE_URL")
SUPABASE_KEY = os.environ.get("SUPABASE_SERVICE_ROLE_KEY")


def get_supabase_client():
    """Lazy init so importing this module never raises on missing credentials."""
    if not SUPABASE_URL or not SUPABASE_KEY:
        raise RuntimeError(
            "❌ SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY မရှိပါ။ "
            "Environment variables ကို စစ်ဆေးပါ။"
        )
    from supabase import create_client
    return create_client(SUPABASE_URL, SUPABASE_KEY)


def create_server():
    """Build the MCP server. Imported lazily so `mcp`/`uvicorn` are optional."""
    from mcp.server.fastmcp import FastMCP

    mcp = FastMCP("Myanmar Intelligent Newsroom")

    @mcp.tool()
    def search_news(query: str, limit: int = 5) -> str:
        """Search verified news articles from Supabase Newsroom DB by keyword."""
        try:
            res = get_supabase_client().rpc(
                "fn_search_news", {"p_query": query, "p_limit": limit}
            ).execute()
            return json.dumps(res.data, ensure_ascii=False)
        except Exception as e:
            return f"Error searching news: {str(e)}"

    @mcp.tool()
    def get_event_timeline(keyword: str) -> str:
        """Get chronological event timeline for any event or news storyline by keyword or Event ID."""
        try:
            res = get_supabase_client().rpc(
                "fn_get_event_timeline", {"p_keyword": keyword}
            ).execute()
            return json.dumps(res.data, ensure_ascii=False)
        except Exception as e:
            return f"Error fetching timeline: {str(e)}"

    @mcp.tool()
    def compare_news_sources(keyword: str) -> str:
        """Compare reporting differences between Myanma Alinn and The Mirror for any topic."""
        try:
            res = get_supabase_client().rpc(
                "fn_compare_sources", {"p_keyword": keyword}
            ).execute()
            return json.dumps(res.data, ensure_ascii=False)
        except Exception as e:
            return f"Error comparing sources: {str(e)}"

    @mcp.tool()
    def get_daily_briefing(date_str: str) -> str:
        """Get top daily intelligence briefing articles for a specific date (YYYY-MM-DD)."""
        try:
            res = get_supabase_client().rpc(
                "fn_get_daily_briefing", {"p_date": date_str}
            ).execute()
            return json.dumps(res.data, ensure_ascii=False)
        except Exception as e:
            return f"Error fetching briefing: {str(e)}"

    return mcp


if __name__ == "__main__":
    # Run as SSE server for Gemini Spark
    import uvicorn

    port = int(os.environ.get("PORT", 8000))
    create_server().run(transport="sse", host="0.0.0.0", port=port)
