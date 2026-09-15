"""Tests for gemini_config — shared Gemini model defaults + fallback chain.

No network, no API keys: Gemini is replaced by a tiny fake client.
"""

import importlib
import re
import unittest
from pathlib import Path
from types import SimpleNamespace

import gemini_config


class _FakeModels:
    """Scripted stand-in for ``client.models``.

    ``script`` maps model name -> response text | Exception | None (empty answer).
    """

    def __init__(self, script):
        self.script = script
        self.calls = []

    def generate_content(self, *, model, contents, config=None):
        self.calls.append(model)
        outcome = self.script.get(model, RuntimeError(f"unexpected model {model}"))
        if isinstance(outcome, Exception):
            raise outcome
        return SimpleNamespace(text=outcome)


def _fake_client(script):
    return SimpleNamespace(models=_FakeModels(script))


class EnvLoadingTests(unittest.TestCase):
    def test_defaults_are_current_generation_models(self):
        primary, fallbacks = gemini_config.load_from_env({})
        self.assertEqual(primary, "gemini-3.5-flash-lite")
        self.assertEqual(fallbacks, ["gemini-3.6-flash", "gemini-flash-latest"])
        for name in [primary, *fallbacks]:
            self.assertNotRegex(name, r"gemini-[12]\.", f"legacy default: {name}")

    def test_blank_values_fall_back_to_defaults(self):
        # The workflow forwards `${{ secrets.GEMINI_MODEL }}` which is "" when unset.
        primary, fallbacks = gemini_config.load_from_env(
            {"GEMINI_MODEL": "   ", "GEMINI_FALLBACK_MODELS": ""}
        )
        self.assertEqual(primary, gemini_config.DEFAULT_GEMINI_MODEL)
        self.assertEqual(fallbacks, list(gemini_config.DEFAULT_FALLBACK_MODELS))

    def test_env_override_and_csv_parsing(self):
        primary, fallbacks = gemini_config.load_from_env(
            {
                "GEMINI_MODEL": " gemini-3.7-flash ",
                "GEMINI_FALLBACK_MODELS": "gemini-3.8-flash, gemini-3.6-flash,,gemini-3.8-flash ",
            }
        )
        self.assertEqual(primary, "gemini-3.7-flash")
        self.assertEqual(fallbacks, ["gemini-3.8-flash", "gemini-3.6-flash"])

    def test_resolve_models_puts_primary_first_and_dedupes(self):
        chain = gemini_config.resolve_models("gemini-3.6-flash", ["gemini-3.6-flash", "gemini-flash-latest"])
        self.assertEqual(chain, ["gemini-3.6-flash", "gemini-flash-latest"])
        # None -> configured chain
        self.assertEqual(gemini_config.resolve_models(None), gemini_config.GEMINI_MODELS)
        # Explicit primary keeps the configured fallbacks
        chain = gemini_config.resolve_models("custom-model")
        self.assertEqual(chain[0], "custom-model")
        self.assertEqual(chain[1:], [m for m in gemini_config.GEMINI_FALLBACK_MODELS if m != "custom-model"])

    def test_describe_models_is_human_readable(self):
        self.assertEqual(
            gemini_config.describe_models(["a", "b"]), "a -> b"
        )


class FallbackTests(unittest.TestCase):
    CHAIN = ["primary", "second", "third"]

    def test_returns_first_successful_model(self):
        client = _fake_client({"primary": "ok"})
        res = gemini_config.generate_content_with_fallback(client, "prompt", models=self.CHAIN)
        self.assertEqual(res.text, "ok")
        self.assertEqual(client.models.calls, ["primary"])

    def test_falls_back_on_exception_and_empty_response(self):
        client = _fake_client(
            {
                "primary": RuntimeError("429 RESOURCE_EXHAUSTED"),
                "second": "   ",  # empty answer counts as a failure
                "third": '[{"headline": "h"}]',
            }
        )
        res = gemini_config.generate_content_with_fallback(client, "prompt", models=self.CHAIN)
        self.assertEqual(res.text, '[{"headline": "h"}]')
        self.assertEqual(client.models.calls, ["primary", "second", "third"])

    def test_parse_failure_moves_to_next_model(self):
        import json

        client = _fake_client({"primary": "not json", "second": '{"a": 1}'})
        data = gemini_config.generate_content_with_fallback(
            client, "prompt", models=["primary", "second"], parse=json.loads
        )
        self.assertEqual(data, {"a": 1})
        self.assertEqual(client.models.calls, ["primary", "second"])

    def test_raises_last_error_when_every_model_fails(self):
        client = _fake_client(
            {"primary": RuntimeError("first"), "second": ValueError("last")}
        )
        with self.assertRaises(ValueError) as ctx:
            gemini_config.generate_content_with_fallback(client, "prompt", models=["primary", "second"])
        self.assertEqual(str(ctx.exception), "last")

    def test_uses_configured_chain_by_default(self):
        script = {m: RuntimeError("down") for m in gemini_config.GEMINI_MODELS}
        script[gemini_config.GEMINI_MODELS[-1]] = "ok"
        client = _fake_client(script)
        gemini_config.generate_content_with_fallback(client, "prompt")
        self.assertEqual(client.models.calls, gemini_config.GEMINI_MODELS)

    def test_empty_chain_is_rejected(self):
        with self.assertRaises(RuntimeError):
            gemini_config.generate_content_with_fallback(_fake_client({}), "prompt", models=[])


class ConsistencyTests(unittest.TestCase):
    """Every Gemini caller must read the same defaults — the drift this fixes."""

    MODULES = ("ingest_engine", "telegram_bot", "cross_source_verifier")

    def test_all_modules_share_the_configured_primary_model(self):
        for name in self.MODULES:
            with self.subTest(module=name):
                module = importlib.import_module(name)
                self.assertEqual(module.GEMINI_MODEL, gemini_config.GEMINI_MODEL)
                self.assertEqual(module.GEMINI_MODELS, gemini_config.GEMINI_MODELS)

    def test_numeric_extractor_defaults_to_shared_chain(self):
        import auto_numeric_extractor

        client = _fake_client({gemini_config.GEMINI_MODEL: "[]"})
        auto_numeric_extractor.extract_numbers_from_article(
            article_text="ရွှေတစ်ကျပ်သား ၇,၁၅၀,၀၀၀ ကျပ်",
            headline="h",
            publication_date="2026-09-15",
            section="s",
            genai_client=client,
        )
        self.assertEqual(client.models.calls, [gemini_config.GEMINI_MODEL])

    def test_no_legacy_gemini_model_names_in_source(self):
        root = Path(__file__).resolve().parent.parent
        offenders = []
        for path in root.glob("*.py"):
            text = path.read_text(encoding="utf-8")
            if re.search(r"[\"']gemini-[12]\.\d", text):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"legacy Gemini model names hard-coded in: {offenders}")

    def test_workflow_does_not_duplicate_the_default(self):
        workflow = (Path(__file__).resolve().parent.parent / ".github/workflows/daily_download.yml").read_text(
            encoding="utf-8"
        )
        self.assertNotIn("|| 'gemini-", workflow, "workflow should defer the default to gemini_config.py")
        self.assertIn("GEMINI_FALLBACK_MODELS", workflow)


if __name__ == "__main__":
    unittest.main()
