from __future__ import annotations

import unittest

from pigeon.worker import _downgrade_interactive_shell_flag, _normalize_route, _route_matches


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

    def test_downgrade_interactive_shell_flag(self) -> None:
        self.assertEqual(
            _downgrade_interactive_shell_flag(["bash", "-ic", "echo hi"]),
            ["bash", "-c", "echo hi"],
        )
        self.assertEqual(
            _downgrade_interactive_shell_flag(["bash", "-ilc", "echo hi"]),
            ["bash", "-lc", "echo hi"],
        )
        self.assertEqual(
            _downgrade_interactive_shell_flag(["bash", "-lc", "echo hi"]),
            ["bash", "-lc", "echo hi"],
        )


if __name__ == "__main__":
    unittest.main()
