"""Environment loading must not depend on which module is imported first.

``utils.VERIFY_TLS`` is computed from ``NEWSROOM_INSECURE_TLS`` at import time,
so it is frozen by whichever module imports ``utils`` first. ``main.py`` imports
``utils`` on an earlier line than ``ingest_engine`` — and ``ingest_engine`` used
to be the only module calling ``load_dotenv()``. A ``.env`` containing
``NEWSROOM_INSECURE_TLS=1`` was therefore silently ignored by the whole download
pipeline: ``utils.VERIFY_TLS`` stayed ``True``, and the flag that exists to get
past a broken TLS proxy did nothing, with no warning anywhere.

Measured before the fix, from the repo root with ``NEWSROOM_INSECURE_TLS=1`` in
``.env``::

    python -c "import main, utils; print(utils.VERIFY_TLS)"      -> True   (bug)
    python -c "import ingest_engine, utils; print(...)"          -> False
    NEWSROOM_INSECURE_TLS=1 python -c "import main, utils; ..."  -> False

The shell-set case working is what pinned the cause to *import order* rather
than to ``env_config``.
"""

import ast
import importlib.util
import inspect
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import main

REPO_ROOT = Path(main.__file__).resolve().parent

# The minimum needed to import utils. Deliberately not the whole repo: utils
# must not drag in the ingest stack (google-genai, supabase, pdfplumber).
UTILS_DEPS = ("utils.py", "env_config.py")


class DotenvLoadingTests(unittest.TestCase):
    """Cold-import a throwaway copy of ``utils`` and report its VERIFY_TLS.

    Executed from a temporary directory holding a copy of ``utils.py`` plus a
    hand-written ``.env``, so ``load_dotenv()``'s frame-based search resolves to
    that ``.env`` and nothing else. A fresh module object is what makes this a
    *cold* import — the value is recomputed from scratch, exactly as it would be
    on a real ``python main.py``.
    """

    def _cold_import(self, dotenv_contents):
        """Return ``(VERIFY_TLS, probe_marker)`` for the isolated copy.

        The probe marker proves the ``.env`` was actually located; without it the
        "defaults on" assertion could pass simply because no ``.env`` was found.
        """
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            for name in UTILS_DEPS:
                shutil.copy(REPO_ROOT / name, root / name)
            (root / ".env").write_text(dotenv_contents, encoding="utf-8")

            # patch.dict restores os.environ afterwards, including the entries
            # load_dotenv() adds from the copy's .env.
            with patch.dict(os.environ, clear=False):
                os.environ.pop("NEWSROOM_INSECURE_TLS", None)
                os.environ.pop("NEWSROOM_PROBE_MARKER", None)
                spec = importlib.util.spec_from_file_location(
                    "cold_utils", root / "utils.py"
                )
                module = importlib.util.module_from_spec(spec)
                spec.loader.exec_module(module)
                return module.VERIFY_TLS, os.environ.get("NEWSROOM_PROBE_MARKER")

    def test_dotenv_opt_out_is_honoured_when_utils_is_imported_first(self):
        verify_tls, marker = self._cold_import(
            "NEWSROOM_INSECURE_TLS=1\nNEWSROOM_PROBE_MARKER=loaded\n"
        )
        self.assertEqual(marker, "loaded", "precondition: the .env file was found")
        self.assertFalse(
            verify_tls,
            "NEWSROOM_INSECURE_TLS=1 in .env must turn verification off; utils "
            "has to load .env itself instead of trusting the importer's order",
        )

    def test_verification_defaults_on_without_the_opt_out(self):
        verify_tls, marker = self._cold_import("NEWSROOM_PROBE_MARKER=loaded\n")
        self.assertEqual(marker, "loaded", "precondition: the .env file was found")
        self.assertTrue(verify_tls, "TLS verification must stay ON by default")


class EntryPointEnvOrderTests(unittest.TestCase):
    def test_main_loads_dotenv_before_importing_local_modules(self):
        """Static guard: the entry point must establish the environment first.

        Any local module that freezes an env value at import time inherits the
        import order of whoever pulls it in, so ``main.py`` must call
        ``load_dotenv()`` above its own local imports rather than relying on a
        transitive load from ``ingest_engine``.
        """
        local_modules = {
            "database",
            "notifications",
            "utils",
            "ingest_engine",
            "env_config",
            "number_utils",
            "gemini_config",
        }
        tree = ast.parse(inspect.getsource(main))

        load_lines = [
            node.lineno
            for node in ast.walk(tree)
            if isinstance(node, ast.Call)
            and isinstance(node.func, ast.Name)
            and node.func.id == "load_dotenv"
        ]
        first_local_import = next(
            (
                node.lineno
                for node in tree.body
                if isinstance(node, ast.ImportFrom) and node.module in local_modules
            ),
            None,
        )

        self.assertTrue(load_lines, "main.py never calls load_dotenv()")
        self.assertIsNotNone(
            first_local_import, "precondition: main.py imports local modules"
        )
        self.assertLess(
            min(load_lines),
            first_local_import,
            f"load_dotenv() at line {min(load_lines)} must precede the first "
            f"local import at line {first_local_import}",
        )


if __name__ == "__main__":
    unittest.main()
