"""Regression tests for base and optional dependency profiles."""

from __future__ import annotations

import importlib.util
import inspect
from importlib.metadata import version
from pathlib import Path
import subprocess
import sys
import unittest
from unittest.mock import patch

import openalex_sdg


ROOT = Path(__file__).resolve().parent
OPTIONAL_PACKAGES = {"scholarly", "httpx", "free-proxy"}


def requirement_names(path: Path) -> set[str]:
    names = set()
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.split("#", 1)[0].strip()
        if not line or line.startswith("-"):
            continue
        names.add(line.split("=", 1)[0].split("<", 1)[0].split(">", 1)[0].lower())
    return names


class DependencyProfileTests(unittest.TestCase):
    def test_base_profile_excludes_optional_scholarly_stack(self) -> None:
        base_names = requirement_names(ROOT / "requirements.txt")
        optional_names = requirement_names(ROOT / "requirements-scholarly.txt")

        self.assertTrue(OPTIONAL_PACKAGES.isdisjoint(base_names))
        self.assertTrue(OPTIONAL_PACKAGES.issubset(optional_names))

    def test_openalex_module_import_does_not_import_scholarly(self) -> None:
        code = """
import builtins
real_import = builtins.__import__
def guarded_import(name, *args, **kwargs):
    if name == 'scholarly' or name.startswith('scholarly.'):
        raise RuntimeError('scholarly was imported eagerly')
    return real_import(name, *args, **kwargs)
builtins.__import__ = guarded_import
import openalex_sdg
"""
        result = subprocess.run(
            [sys.executable, "-c", code],
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)

    def test_unavailable_scholarly_fallback_is_skipped_before_import(self) -> None:
        with patch.object(openalex_sdg.importlib.util, "find_spec", return_value=None):
            self.assertFalse(openalex_sdg.scholarly_fallback_available())
            self.assertIsNone(openalex_sdg.get_abstract_from_scholarly("A title", ""))

    @unittest.skipUnless(
        importlib.util.find_spec("scholarly"),
        "optional scholarly profile is not installed",
    )
    def test_installed_optional_profile_matches_compatibility_constraints(self) -> None:
        import httpx
        from fp.fp import FreeProxy

        self.assertEqual(version("scholarly"), "1.7.11")
        self.assertEqual(version("free-proxy"), "1.0.6")
        self.assertLess(tuple(map(int, version("httpx").split("."))), (0, 28, 0))
        self.assertIn("proxies", inspect.signature(httpx.Client).parameters)
        self.assertEqual(list(inspect.signature(FreeProxy.get_proxy_list).parameters), ["self"])


if __name__ == "__main__":
    unittest.main()
