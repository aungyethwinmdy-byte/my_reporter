"""Static wiring guards — catch "the symbol exists but the name is wrong" bugs.

Why this file exists
--------------------
``telegram_bot.compare_command_handler`` guarded on ``run_cross_source_verifier``
while the module actually imported ``run_cross_source_comparison``.  The module
still imported cleanly and every unit test passed, because the broken name only
resolved at call time — so ``/compare`` raised ``NameError`` for real users while
CI stayed green.  ``ruff`` now flags it as F821, and these tests pin the contract
so a plain ``python -m unittest`` run catches it too (the suite is the gate the
GitHub workflow actually runs).

No network, no API keys — everything here is introspection.
"""

import ast
import importlib
import re
import unittest
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Every application module. Keep in sync with tests/test_main.py ModuleImportTests.
APP_MODULES = (
    "database",
    "notifications",
    "utils",
    "ingest_engine",
    "main",
    "auto_numeric_extractor",
    "cross_source_verifier",
    "fetch_independent_news",
    "gemini_config",
    "numeric_intelligence_engine",
    "report_formatter",
    "system_router",
    "telegram_bot",
)


def _module_level_bound_names(tree):
    """Names bound anywhere at module scope, including inside ``try``/``with``/``if``.

    ``main.py`` imports the Google Drive symbols inside a ``try: ... except
    ImportError:`` block, so a naive "only look at tree.body" scan would report
    ``build`` / ``MediaFileUpload`` as undefined. Walk every module-level
    statement (and the bodies of module-level compound statements) instead.
    """
    bound = set()
    for node in tree.body:
        for child in ast.walk(node):
            if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                bound.add(child.id)
            elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                bound.add(child.name)
            elif isinstance(child, ast.arg):
                bound.add(child.arg)
            elif isinstance(child, (ast.Import, ast.ImportFrom)):
                for alias in child.names:
                    bound.add(alias.asname or alias.name.split(".")[0])
            elif isinstance(child, ast.ExceptHandler) and child.name:
                bound.add(child.name)
    return bound


class UndefinedNameTests(unittest.TestCase):
    """A function body must not reference a name the module never binds.

    This is the exact shape of the ``/compare`` bug: a typo in a ``if not X:``
    guard that no import-time check would ever surface.
    """

    def test_app_modules_have_no_undefined_module_level_names(self):
        for module_name in APP_MODULES:
            with self.subTest(module=module_name):
                module = importlib.import_module(module_name)

                source_path = Path(module.__file__)
                tree = ast.parse(source_path.read_text(encoding="utf-8"), filename=str(source_path))

                # Names bound at module scope, including conditionally-imported
                # ones (e.g. the optional Google Drive imports in main.py).
                bound = _module_level_bound_names(tree)
                # Plus anything the module legitimately sees from builtins
                import builtins

                bound |= set(dir(builtins))

                offenders = set()
                # Only inspect module-level functions: nested scopes have their
                # own locals which this simple analysis does not model.
                for node in tree.body:
                    if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                        continue
                    local_names = set()
                    for child in ast.walk(node):
                        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Store):
                            local_names.add(child.id)
                        elif isinstance(child, ast.arg):
                            local_names.add(child.arg)
                        elif isinstance(child, (ast.FunctionDef, ast.AsyncFunctionDef)) and child is not node:
                            local_names.add(child.name)
                        elif isinstance(child, ast.ExceptHandler) and child.name:
                            local_names.add(child.name)
                        elif isinstance(child, (ast.Import, ast.ImportFrom)):
                            for alias in child.names:
                                local_names.add(alias.asname or alias.name.split(".")[0])

                    for child in ast.walk(node):
                        if isinstance(child, ast.Name) and isinstance(child.ctx, ast.Load):
                            if child.id not in bound and child.id not in local_names:
                                offenders.add(child.id)

                self.assertEqual(
                    offenders,
                    set(),
                    f"{module_name} references unbound name(s): {sorted(offenders)}",
                )


class CrossSourceVerifierWiringTests(unittest.TestCase):
    """The /compare handler must guard on, and call, the symbol it imported."""

    def test_verifier_symbol_is_importable_and_callable(self):
        module = importlib.import_module("cross_source_verifier")
        self.assertTrue(
            callable(getattr(module, "run_cross_source_comparison", None)),
            "cross_source_verifier.run_cross_source_comparison must exist",
        )

    def test_telegram_bot_binds_the_symbol_it_uses(self):
        bot = importlib.import_module("telegram_bot")
        self.assertIsNotNone(
            bot.run_cross_source_comparison,
            "telegram_bot must import run_cross_source_comparison (verifier available)",
        )

    def test_compare_handler_references_only_defined_verifier_names(self):
        import inspect

        bot = importlib.import_module("telegram_bot")
        source = inspect.getsource(bot.compare_command_handler)
        referenced = set(re.findall(r"\brun_cross_source_\w+", source))

        self.assertIn("run_cross_source_comparison", referenced)
        self.assertNotIn(
            "run_cross_source_verifier",
            referenced,
            "stale symbol name — the verifier is imported as run_cross_source_comparison",
        )

    def test_no_stale_verifier_name_anywhere_in_source(self):
        offenders = []
        for path in PROJECT_ROOT.glob("*.py"):
            if "run_cross_source_verifier" in path.read_text(encoding="utf-8"):
                offenders.append(path.name)
        self.assertEqual(offenders, [], f"stale name present in: {offenders}")


class CommandHandlerRegistrationTests(unittest.TestCase):
    """Registered commands must point at callable handlers.

    ``main()`` builds the Application, so we assert the handler callables exist
    and are coroutine functions rather than booting python-telegram-bot.
    """

    HANDLERS = (
        "start_handler",
        "sources_command_handler",
        "status_command_handler",
        "compare_command_handler",
        "message_handler",
        "error_handler",
    )

    def test_all_registered_handlers_exist(self):
        import inspect

        bot = importlib.import_module("telegram_bot")
        for name in self.HANDLERS:
            with self.subTest(handler=name):
                handler = getattr(bot, name, None)
                self.assertTrue(callable(handler), f"telegram_bot.{name} must be callable")
                self.assertTrue(
                    inspect.iscoroutinefunction(handler),
                    f"telegram_bot.{name} must be async (python-telegram-bot awaits it)",
                )


if __name__ == "__main__":
    unittest.main()
