"""Shared test helpers: make any accidental network call fail loudly."""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))


class NoNetworkTestCase(unittest.TestCase):
    def setUp(self) -> None:
        import urllib.request

        def explode(*args, **kwargs):
            raise AssertionError("network access attempted during an offline test")

        patcher = mock.patch.object(urllib.request, "urlopen", explode)
        patcher.start()
        self.addCleanup(patcher.stop)
