"""Tests for newsroom_mcp — the four MCP tools exposed to an LLM client.

This module had no functional tests at all, only an import check. `mcp` is an
optional dependency and is not installed, so `create_server()` is exercised by
injecting a fake `mcp.server.fastmcp` that records the registered tools.

No network, no Supabase, no credentials.
"""

import json
import sys
import types
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import newsroom_mcp
from newsroom_mcp import (
    DEFAULT_LIST_LIMIT,
    DEFAULT_SEARCH_LIMIT,
    MAX_LIST_LIMIT,
    MAX_SEARCH_LIMIT,
    _clamp_limit,
    _clean_term,
    _is_iso_date,
)


class _FakeFastMCP:
    def __init__(self, name):
        self.name = name
        self.tools = {}

    def tool(self):
        def decorator(func):
            self.tools[func.__name__] = func
            return func

        return decorator


class _FakeRpc:
    def __init__(self, data):
        self.data = data

    def execute(self):
        return SimpleNamespace(data=self.data)


class _FakeClient:
    """Records every RPC call so tests can assert on the exact parameters."""

    def __init__(self, data=None, error=None):
        self.data = data
        self.error = error
        self.calls = []

    def rpc(self, name, params):
        self.calls.append((name, params))
        if self.error is not None:
            raise self.error
        return _FakeRpc(self.data)


def _build_tools():
    """Return the tool functions create_server() registers, without `mcp`."""
    fastmcp_module = types.ModuleType("mcp.server.fastmcp")
    created = {}

    def FastMCP(name):
        created["server"] = _FakeFastMCP(name)
        return created["server"]

    fastmcp_module.FastMCP = FastMCP
    server_module = types.ModuleType("mcp.server")
    server_module.fastmcp = fastmcp_module
    mcp_module = types.ModuleType("mcp")
    mcp_module.server = server_module

    with patch.dict(
        sys.modules,
        {
            "mcp": mcp_module,
            "mcp.server": server_module,
            "mcp.server.fastmcp": fastmcp_module,
        },
    ):
        newsroom_mcp.create_server()
    return created["server"].tools


class DotenvLoadingTests(unittest.TestCase):
    def test_env_is_loaded_before_credentials_are_read(self):
        """The only entry point that did not load .env.

        `python newsroom_mcp.py` on a .env-configured machine started the server
        and then failed every tool call with "SUPABASE_URL ... မရှိပါ", because
        the credentials are read at import time and nothing had populated them.
        Order matters: load_dotenv() must run first.

        Compared via AST rather than str.index(), because the prose comments in
        this module name both functions and would otherwise match first.
        """
        import ast

        tree = ast.parse(Path(newsroom_mcp.__file__).read_text(encoding="utf-8"))
        lines = {}
        for node in ast.walk(tree):
            if isinstance(node, ast.Call):
                name = getattr(node.func, "id", None) or getattr(node.func, "attr", None)
                if name in ("load_dotenv", "get_supabase_credentials"):
                    lines.setdefault(name, node.lineno)

        self.assertIn("load_dotenv", lines, "load_dotenv() is never called")
        self.assertIn("get_supabase_credentials", lines)
        self.assertLess(
            lines["load_dotenv"],
            lines["get_supabase_credentials"],
            "credentials are read before .env is loaded",
        )


class ClampLimitTests(unittest.TestCase):
    """Nothing caps p_limit server-side; p_limit=1000000 returned everything."""

    def test_passes_through_a_sane_value(self):
        self.assertEqual(_clamp_limit(5), 5)
        self.assertEqual(_clamp_limit(1), 1)

    def test_caps_an_enormous_value(self):
        self.assertEqual(_clamp_limit(1_000_000), MAX_SEARCH_LIMIT)

    def test_rejects_negative_and_zero(self):
        """Postgres raises 2201W on a negative LIMIT."""
        self.assertEqual(_clamp_limit(-1), 1)
        self.assertEqual(_clamp_limit(0), 1)

    def test_coerces_json_supplied_types(self):
        self.assertEqual(_clamp_limit("7"), 7)
        self.assertEqual(_clamp_limit(3.9), 3)

    def test_falls_back_on_junk(self):
        self.assertEqual(_clamp_limit(None), DEFAULT_SEARCH_LIMIT)
        self.assertEqual(_clamp_limit("abc"), DEFAULT_SEARCH_LIMIT)
        self.assertEqual(_clamp_limit({}), DEFAULT_SEARCH_LIMIT)


class CleanTermTests(unittest.TestCase):
    def test_trims(self):
        self.assertEqual(_clean_term("  ရွှေ  "), "ရွှေ")

    def test_blank_forms_collapse_to_empty(self):
        self.assertEqual(_clean_term(""), "")
        self.assertEqual(_clean_term("   "), "")
        self.assertEqual(_clean_term(None), "")


class IsIsoDateTests(unittest.TestCase):
    def test_accepts_real_dates(self):
        self.assertTrue(_is_iso_date("2026-09-18"))

    def test_rejects_impossible_dates(self):
        self.assertFalse(_is_iso_date("2026-13-45"))
        self.assertFalse(_is_iso_date("2026-02-30"))

    def test_rejects_other_formats_and_blanks(self):
        for value in ("18/09/2026", "2026-9-8", "", "   ", None, "today"):
            with self.subTest(value=value):
                self.assertFalse(_is_iso_date(value))


class ToolTests(unittest.TestCase):
    def setUp(self):
        self.tools = _build_tools()
        self.client = _FakeClient(data=[{"headline": "h"}])
        patcher = patch.object(newsroom_mcp, "get_supabase_client", return_value=self.client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _payload(self, raw):
        return json.loads(raw)

    def test_all_four_tools_are_registered(self):
        self.assertEqual(
            sorted(self.tools),
            ["compare_news_sources", "get_daily_briefing", "get_event_timeline", "search_news"],
        )

    def test_search_news_passes_query_and_limit(self):
        self.tools["search_news"]("ရွှေ", 3)
        self.assertEqual(self.client.calls, [("fn_search_news", {"p_query": "ရွှေ", "p_limit": 3})])

    def test_search_news_clamps_the_limit(self):
        self.tools["search_news"]("ရွှေ", 1_000_000)
        self.assertEqual(self.client.calls[0][1]["p_limit"], MAX_SEARCH_LIMIT)

    def test_search_news_rejects_a_blank_query_without_calling_the_db(self):
        for blank in ("", "   ", None):
            with self.subTest(blank=blank):
                self.client.calls.clear()
                payload = self._payload(self.tools["search_news"](blank))
                self.assertIn("error", payload)
                self.assertEqual(self.client.calls, [], "must not hit the database")

    def test_timeline_and_compare_reject_a_blank_keyword(self):
        """fn_get_event_timeline('') returned 1000 rows — the whole table."""
        for name in ("get_event_timeline", "compare_news_sources"):
            with self.subTest(tool=name):
                self.client.calls.clear()
                payload = self._payload(self.tools[name](""))
                self.assertIn("error", payload)
                self.assertEqual(self.client.calls, [])

    def test_daily_briefing_rejects_a_bad_date_without_calling_the_db(self):
        """p_date is a Postgres date, so junk raised 22007 from the database."""
        for bad in ("", "yesterday", "2026-13-01", "18/09/2026"):
            with self.subTest(bad=bad):
                self.client.calls.clear()
                payload = self._payload(self.tools["get_daily_briefing"](bad))
                self.assertIn("error", payload)
                self.assertEqual(self.client.calls, [])

    def test_daily_briefing_passes_a_valid_date(self):
        self.tools["get_daily_briefing"]("2026-09-18")
        self.assertEqual(self.client.calls, [("fn_get_daily_briefing", {"p_date": "2026-09-18"})])

    def test_success_returns_the_rows_as_json(self):
        payload = self._payload(self.tools["search_news"]("ရွှေ"))
        self.assertEqual(payload, [{"headline": "h"}])

    def test_list_tools_cap_the_row_count(self):
        """These three RPCs take no limit server-side.

        Measured against production, one call returned 442 articles / 1150 KB /
        ~295,000 tokens — about 30% of a 1M-token window, and an outright
        overflow for a 128k model. Rows are capped in Python instead.
        """
        self.client.data = [{"i": i} for i in range(100)]
        for name, arg in (
            ("get_event_timeline", "ရွှေ"),
            ("compare_news_sources", "ရွှေ"),
            ("get_daily_briefing", "2026-09-18"),
        ):
            with self.subTest(tool=name):
                rows = self._payload(self.tools[name](arg))
                self.assertEqual(len(rows), DEFAULT_LIST_LIMIT)

    def test_list_tools_honour_and_clamp_an_explicit_limit(self):
        self.client.data = [{"i": i} for i in range(100)]
        self.assertEqual(len(self._payload(self.tools["get_event_timeline"]("ရွှေ", 3))), 3)
        self.assertEqual(
            len(self._payload(self.tools["get_event_timeline"]("ရွှေ", 1_000_000))),
            MAX_LIST_LIMIT,
        )
        self.assertEqual(len(self._payload(self.tools["get_daily_briefing"]("2026-09-18", 7))), 7)

    def test_list_tools_do_not_send_a_limit_to_the_database(self):
        """The RPC signatures have no limit argument, so one must not be invented."""
        self.client.data = [{"i": 0}]
        self.tools["get_daily_briefing"]("2026-09-18", 5)
        self.assertEqual(self.client.calls, [("fn_get_daily_briefing", {"p_date": "2026-09-18"})])


class ErrorHandlingTests(unittest.TestCase):
    """Failures must not look like news, and must not leak database internals."""

    def setUp(self):
        self.tools = _build_tools()

    def _with_error(self, error):
        client = _FakeClient(error=error)
        patcher = patch.object(newsroom_mcp, "get_supabase_client", return_value=client)
        patcher.start()
        self.addCleanup(patcher.stop)

    def test_failure_returns_a_structured_error(self):
        self._with_error(RuntimeError("boom"))
        payload = json.loads(self.tools["search_news"]("ရွှေ"))
        self.assertIsInstance(payload, dict)
        self.assertIn("error", payload)

    def test_raw_database_text_is_not_echoed_to_the_model(self):
        """The model would otherwise quote the schema error as if it were news."""
        self._with_error(
            RuntimeError('relation "newspaper_articles" does not exist')
        )
        raw = self.tools["search_news"]("ရွှေ")
        self.assertNotIn("newspaper_articles", raw)
        self.assertNotIn("does not exist", raw)

    def test_client_construction_failure_is_also_structured(self):
        patcher = patch.object(
            newsroom_mcp,
            "get_supabase_client",
            side_effect=RuntimeError("❌ SUPABASE_URL ... မရှိပါ။"),
        )
        patcher.start()
        self.addCleanup(patcher.stop)
        payload = json.loads(self.tools["get_event_timeline"]("ရွှေ"))
        self.assertIn("error", payload)


if __name__ == "__main__":
    unittest.main()
