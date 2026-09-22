"""Tests for env_config — validated environment readers.

These exist because the old inline `int(os.getenv(...))` calls parsed at import
time and crashed the module on any malformed value. A typo in a CI secret must
degrade to a warning + default, never to a dead process.
"""

import unittest

from env_config import (
    get_bool,
    get_float,
    get_gemini_api_key,
    get_int,
    get_str,
    get_supabase_credentials,
)


class SupabaseCredentialTests(unittest.TestCase):
    """The key fallback chain used to be copy-pasted into five modules."""

    def test_service_role_key_wins(self):
        env = {"SUPABASE_URL": "u", "SUPABASE_SERVICE_ROLE_KEY": "sr", "SUPABASE_KEY": "k"}
        self.assertEqual(get_supabase_credentials(env), ("u", "sr"))

    def test_each_key_var_works_alone(self):
        for var in ("SUPABASE_SERVICE_ROLE_KEY", "SUPABASE_KEY", "SUPABASE_ANON_KEY"):
            with self.subTest(var=var):
                env = {"SUPABASE_URL": "u", var: "secret"}
                self.assertEqual(get_supabase_credentials(env), ("u", "secret"))

    def test_fallback_priority(self):
        env = {"SUPABASE_URL": "u", "SUPABASE_KEY": "k", "SUPABASE_ANON_KEY": "an"}
        self.assertEqual(get_supabase_credentials(env), ("u", "k"))

    def test_missing_key_yields_none(self):
        self.assertEqual(get_supabase_credentials({"SUPABASE_URL": "u"}), ("u", None))

    def test_unconfigured_yields_none_pair(self):
        self.assertEqual(get_supabase_credentials({}), (None, None))

    def test_blank_values_count_as_unset(self):
        self.assertEqual(
            get_supabase_credentials({"SUPABASE_URL": "   ", "SUPABASE_KEY": "  "}),
            (None, None),
        )


class GeminiKeyTests(unittest.TestCase):
    def test_prefers_gemini_api_key(self):
        env = {"GEMINI_API_KEY": "g", "GOOGLE_API_KEY": "x"}
        self.assertEqual(get_gemini_api_key(env), "g")

    def test_falls_back_to_google_api_key(self):
        self.assertEqual(get_gemini_api_key({"GOOGLE_API_KEY": "go"}), "go")

    def test_missing_yields_none(self):
        self.assertEqual(get_gemini_api_key({}), None)


class CredentialConsistencyTests(unittest.TestCase):
    """Every module must agree on the resolved credentials."""

    MODULES = (
        "ingest_engine",
        "telegram_bot",
        "cross_source_verifier",
        "fetch_independent_news",
        "newsroom_mcp",
    )

    def test_modules_share_env_config_resolution(self):
        import importlib
        import os

        with self.subTest(module="all"):
            for name in self.MODULES:
                with self.subTest(module=name):
                    module = importlib.import_module(name)
                    self.assertEqual(
                        (module.SUPABASE_URL, module.SUPABASE_KEY),
                        get_supabase_credentials(os.environ),
                        f"{name} resolved Supabase creds differently",
                    )


class GetIntTests(unittest.TestCase):
    def test_valid_value_is_used(self):
        self.assertEqual(get_int("X", 14, environ={"X": "30"}), 30)

    def test_whitespace_is_tolerated(self):
        self.assertEqual(get_int("X", 14, environ={"X": "  30  "}), 30)

    def test_malformed_value_falls_back_to_default(self):
        for bad in ("14 days", "eight", "1.5", "abc", "--3"):
            with self.subTest(value=bad):
                self.assertEqual(get_int("X", 14, environ={"X": bad}), 14)

    def test_missing_or_blank_uses_default(self):
        self.assertEqual(get_int("X", 14, environ={}), 14)
        self.assertEqual(get_int("X", 14, environ={"X": ""}), 14)
        self.assertEqual(get_int("X", 14, environ={"X": "   "}), 14)

    def test_below_minimum_falls_back(self):
        # MOI_MAX_WORKERS=0 would deadlock the probe pool.
        self.assertEqual(get_int("X", 8, minimum=1, environ={"X": "0"}), 8)
        self.assertEqual(get_int("X", 8, minimum=1, environ={"X": "-5"}), 8)

    def test_above_maximum_falls_back(self):
        self.assertEqual(get_int("X", 8, maximum=64, environ={"X": "9999"}), 8)

    def test_within_bounds_is_accepted(self):
        self.assertEqual(get_int("X", 8, minimum=1, maximum=64, environ={"X": "1"}), 1)
        self.assertEqual(get_int("X", 8, minimum=1, maximum=64, environ={"X": "64"}), 64)


class GetFloatTests(unittest.TestCase):
    def test_valid_value_is_used(self):
        self.assertEqual(get_float("X", 8.0, environ={"X": "12.5"}), 12.5)
        self.assertEqual(get_float("X", 8.0, environ={"X": "90"}), 90.0)

    def test_malformed_falls_back(self):
        self.assertEqual(get_float("X", 8.0, environ={"X": "fast"}), 8.0)
        self.assertEqual(get_float("X", 8.0, environ={"X": ""}), 8.0)

    def test_bounds(self):
        self.assertEqual(get_float("X", 8.0, minimum=0.5, environ={"X": "0.1"}), 8.0)
        self.assertEqual(get_float("X", 8.0, maximum=120.0, environ={"X": "999"}), 8.0)

    def test_scientific_notation_accepted(self):
        self.assertEqual(get_float("X", 1.0, environ={"X": "1e3"}), 1000.0)


class GetBoolTests(unittest.TestCase):
    def test_truthy_values(self):
        for raw in ("1", "true", "TRUE", "True", "yes", "YES", "on", "ON", " 1 "):
            with self.subTest(value=raw):
                self.assertTrue(get_bool("X", False, environ={"X": raw}))

    def test_falsy_values(self):
        for raw in ("0", "false", "no", "off", "nonsense", "", "   "):
            with self.subTest(value=raw):
                self.assertFalse(get_bool("X", False, environ={"X": raw}))

    def test_default_used_when_unset(self):
        self.assertTrue(get_bool("X", True, environ={}))
        self.assertFalse(get_bool("X", False, environ={}))


class GetStrTests(unittest.TestCase):
    def test_returns_trimmed_value(self):
        self.assertEqual(get_str("X", "d", environ={"X": "  hello  "}), "hello")

    def test_blank_counts_as_unset(self):
        self.assertEqual(get_str("X", "fallback", environ={"X": "   "}), "fallback")
        self.assertEqual(get_str("X", "fallback", environ={"X": ""}), "fallback")

    def test_missing_uses_default(self):
        self.assertEqual(get_str("X", "fallback", environ={}), "fallback")


class ModuleImportRobustnessTests(unittest.TestCase):
    """The real regression: a bad env value must not break module import."""

    def test_utils_imports_with_malformed_numeric_env(self):
        import importlib
        import os
        import sys

        bad_env = {
            "MOI_MAX_WORKERS": "eight",
            "MOI_PHASE_BUDGET": "ninety",
            "MOI_SLUG_TIMEOUT": "soon",
            "MDN_DATE_TIMEOUT": "later",
        }
        original = {k: os.environ.get(k) for k in bad_env}
        try:
            os.environ.update(bad_env)
            sys.modules.pop("utils", None)
            module = importlib.import_module("utils")
            # Must have fallen back to the documented defaults.
            self.assertEqual(module.MOI_MAX_WORKERS, 8)
            self.assertEqual(module.MOI_PHASE_BUDGET, 90.0)
            self.assertEqual(module.MOI_SLUG_TIMEOUT, 8.0)
            self.assertEqual(module.MDN_DATE_TIMEOUT, 10.0)
        finally:
            for key, value in original.items():
                if value is None:
                    os.environ.pop(key, None)
                else:
                    os.environ[key] = value
            sys.modules.pop("utils", None)
            importlib.import_module("utils")


if __name__ == "__main__":
    unittest.main()
