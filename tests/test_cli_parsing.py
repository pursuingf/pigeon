from __future__ import annotations

import os
import tempfile
import unittest
from contextlib import redirect_stdout
from io import StringIO
from pathlib import Path
from unittest import mock

from pigeon.cli import _split_client_args, main


class CliParsingTests(unittest.TestCase):
    def test_split_client_args_stops_at_first_command_token(self) -> None:
        known, command = _split_client_args(["-v", "codex", "--route", "x"])
        self.assertEqual(known, ["-v"])
        self.assertEqual(command, ["codex", "--route", "x"])

    def test_split_client_args_supports_double_dash_separator(self) -> None:
        known, command = _split_client_args(["-v", "--", "--route", "x"])
        self.assertEqual(known, ["-v"])
        self.assertEqual(command, ["--route", "x"])

    def test_split_client_args_without_client_flags(self) -> None:
        known, command = _split_client_args(["echo", "hello"])
        self.assertEqual(known, [])
        self.assertEqual(command, ["echo", "hello"])

    def test_split_client_args_parses_route(self) -> None:
        known, command = _split_client_args(["--route", "cpu-a", "echo", "ok"])
        self.assertEqual(known, ["--route", "cpu-a"])
        self.assertEqual(command, ["echo", "ok"])

    def test_split_client_args_parses_wait_worker(self) -> None:
        known, command = _split_client_args(["--wait-worker", "1.5", "echo", "ok"])
        self.assertEqual(known, ["--wait-worker", "1.5"])
        self.assertEqual(command, ["echo", "ok"])

    def test_main_config_only_refreshes_target_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Path(tmp) / "cfg.toml"
            env = {"USER": "cli-test-user", "HOME": tmp}
            with mock.patch.dict(os.environ, env, clear=True):
                buf = StringIO()
                with redirect_stdout(buf):
                    rc = main(["config", "path", str(cfg)])
                path_buf = StringIO()
                with redirect_stdout(path_buf):
                    rc_path = main(["config", "path"])
            self.assertEqual(rc, 0)
            self.assertEqual(rc_path, 0)
            self.assertTrue(cfg.exists())
            body = cfg.read_text(encoding="utf-8")
            self.assertIn('cache = "/tmp/pigeon-cache"', body)
            self.assertIn('namespace = "cli-test-user"', body)
            self.assertEqual(path_buf.getvalue().strip(), str(cfg.resolve()))

    def test_main_rejects_removed_config_flag(self) -> None:
        err = StringIO()
        with redirect_stdout(StringIO()):
            with mock.patch("sys.stderr", err):
                rc = main(["--config", "/tmp/x.toml"])
        self.assertEqual(rc, 2)
        self.assertIn("--config", err.getvalue())


if __name__ == "__main__":
    unittest.main()
