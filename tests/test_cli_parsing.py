from __future__ import annotations

import unittest

from pigeon.cli import _split_client_args


class CliParsingTests(unittest.TestCase):
    def test_split_client_args_stops_at_first_command_token(self) -> None:
        known, command = _split_client_args(["--config", "a.toml", "-v", "codex", "--config", "x"])
        self.assertEqual(known, ["--config", "a.toml", "-v"])
        self.assertEqual(command, ["codex", "--config", "x"])

    def test_split_client_args_supports_double_dash_separator(self) -> None:
        known, command = _split_client_args(["-v", "--", "--config", "x"])
        self.assertEqual(known, ["-v"])
        self.assertEqual(command, ["--config", "x"])

    def test_split_client_args_without_client_flags(self) -> None:
        known, command = _split_client_args(["echo", "hello"])
        self.assertEqual(known, [])
        self.assertEqual(command, ["echo", "hello"])

    def test_split_client_args_parses_route(self) -> None:
        known, command = _split_client_args(["--route", "cpu-a", "echo", "ok"])
        self.assertEqual(known, ["--route", "cpu-a"])
        self.assertEqual(command, ["echo", "ok"])


if __name__ == "__main__":
    unittest.main()
