from __future__ import annotations

import unittest

from pigeon.worker import _normalize_route, _route_matches


class WorkerRoutingTests(unittest.TestCase):
    def test_normalize_route(self) -> None:
        self.assertIsNone(_normalize_route(None))
        self.assertIsNone(_normalize_route(""))
        self.assertIsNone(_normalize_route("   "))
        self.assertEqual(_normalize_route(" cpu-a "), "cpu-a")
        self.assertIsNone(_normalize_route(123))

    def test_route_matching(self) -> None:
        self.assertTrue(_route_matches(None, None))
        self.assertTrue(_route_matches("cpu-a", "cpu-a"))
        self.assertFalse(_route_matches("cpu-a", "cpu-b"))
        self.assertFalse(_route_matches(None, "cpu-a"))
        self.assertFalse(_route_matches("cpu-a", None))


if __name__ == "__main__":
    unittest.main()
