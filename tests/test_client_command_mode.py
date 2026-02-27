from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pigeon.client import (
    _build_request,
    _normalize_exec_command,
    _resolve_worker_wait_timeout,
    _shell_prelude,
    _wait_for_worker,
)
from pigeon.common import PigeonConfig, now_ts, write_worker_heartbeat
from pigeon.config import FileConfig


class ClientCommandModeTests(unittest.TestCase):
    @staticmethod
    def _cfg(remote_env: dict[str, str] | None = None) -> FileConfig:
        return FileConfig(
            path=None,
            cache=None,
            namespace=None,
            route=None,
            user=None,
            worker_max_jobs=None,
            worker_poll_interval=None,
            worker_debug=None,
            worker_route=None,
            remote_env=remote_env or {},
        )

    def test_plain_command_is_wrapped_with_clean_bash_c(self) -> None:
        wrapped = _normalize_exec_command(["codex", "--version"], self._cfg())
        self.assertEqual(wrapped[0:4], ["bash", "--noprofile", "--norc", "-c"])
        self.assertEqual(wrapped[4], "codex --version")

    def test_shell_lc_command_is_kept(self) -> None:
        cmd = ["bash", "-lc", "echo hi"]
        wrapped = _normalize_exec_command(cmd, self._cfg())
        self.assertEqual(wrapped, cmd)

    def test_arguments_are_shell_escaped(self) -> None:
        wrapped = _normalize_exec_command(["python", "-c", 'print("a b")'], self._cfg())
        self.assertEqual(wrapped[0:4], ["bash", "--noprofile", "--norc", "-c"])
        self.assertIn("python -c", wrapped[4])
        self.assertIn("'print(\"a b\")'", wrapped[4])

    def test_single_argument_shell_snippet_is_used_verbatim(self) -> None:
        wrapped = _normalize_exec_command(["pwd; echo hi"], self._cfg())
        self.assertEqual(wrapped, ["bash", "--noprofile", "--norc", "-c", "pwd; echo hi"])

    def test_shell_prelude_enables_color_aliases_for_tty(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("pigeon.client.sys.stdout.isatty", return_value=True):
                prelude = _shell_prelude()
        self.assertIn("alias ls='ls --color=auto'\n", prelude)
        self.assertTrue(prelude.endswith("\n"))

    def test_remote_env_from_config_has_highest_priority(self) -> None:
        file_cfg = self._cfg({"FOO": "from_config", "BAR": "bar_cfg"})
        file_cfg = FileConfig(**{**file_cfg.__dict__, "user": "cfg-user"})
        with mock.patch.dict(os.environ, {"FOO": "from_local", "USER": "local-user"}, clear=True):
            req = _build_request(
                command=["bash", "-lc", "echo x"],
                cwd="/tmp",
                session_id="sid",
                file_config=file_cfg,
                route=None,
            )
        env = req["env"]
        self.assertIsInstance(env, dict)
        self.assertEqual(env["FOO"], "from_config")
        self.assertEqual(env["BAR"], "bar_cfg")
        self.assertNotIn("USER", env)
        requester = req["requester"]
        self.assertIsInstance(requester, dict)
        self.assertEqual(requester["user"], "cfg-user")
        self.assertIsNone(req["route"])

    def test_build_request_does_not_forward_local_env(self) -> None:
        with mock.patch.dict(os.environ, {"LOCAL_ONLY": "x", "USER": "local-user"}, clear=True):
            req = _build_request(
                command=["bash", "-lc", "echo x"],
                cwd="/tmp",
                session_id="sid",
                file_config=self._cfg(),
                route=None,
            )
        env = req["env"]
        self.assertIsInstance(env, dict)
        self.assertEqual(env, {})

    def test_build_request_sets_route(self) -> None:
        req = _build_request(
            command=["bash", "-lc", "echo x"],
            cwd="/tmp",
            session_id="sid",
            file_config=self._cfg(),
            route="cpu-a",
        )
        self.assertEqual(req["route"], "cpu-a")

    def test_rewrite_restores_remote_env_ref_when_local_already_expanded(self) -> None:
        file_cfg = self._cfg({"HTTPS_PROXY": "http://proxy.example:8080"})
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://0.0.0.0:7890"}, clear=True):
            wrapped = _normalize_exec_command(
                ["echo", "http://0.0.0.0:7890"],
                file_cfg,
            )
        self.assertEqual(wrapped[0:4], ["bash", "--noprofile", "--norc", "-c"])
        self.assertEqual(wrapped[4], "echo $HTTPS_PROXY")

    def test_rewrite_prefers_inline_assignment_rhs(self) -> None:
        file_cfg = self._cfg({"HTTPS_PROXY": "http://proxy.example:8080"})
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://0.0.0.0:7890"}, clear=True):
            wrapped = _normalize_exec_command(
                ["HTTPS_PROXY=http://proxy.example:8080", "echo", "http://0.0.0.0:7890"],
                file_cfg,
            )
        self.assertEqual(wrapped[0:4], ["bash", "--noprofile", "--norc", "-c"])
        self.assertEqual(wrapped[4], "HTTPS_PROXY=http://proxy.example:8080 echo http://proxy.example:8080")

    def test_wait_worker_timeout_default(self) -> None:
        with mock.patch.dict(os.environ, {}, clear=True):
            timeout = _resolve_worker_wait_timeout(argparse.Namespace(wait_worker=None))
        self.assertAlmostEqual(timeout, 3.0, places=6)

    def test_wait_for_worker_returns_empty_when_none(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = PigeonConfig(cache_root=Path(tmp), namespace="ns")
            cfg.ensure_dirs()
            workers = _wait_for_worker(cfg, route=None, timeout=0.0)
        self.assertEqual(workers, [])

    def test_wait_for_worker_filters_by_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = PigeonConfig(cache_root=Path(tmp), namespace="ns")
            cfg.ensure_dirs()
            now = now_ts()
            write_worker_heartbeat(
                cfg,
                "worker-a",
                route="cpu-a",
                host="h",
                pid=1,
                started_at="2026-01-01T00:00:00.000000Z",
                now=now,
            )
            workers = _wait_for_worker(cfg, route="cpu-a", timeout=0.0)
            missing = _wait_for_worker(cfg, route="cpu-b", timeout=0.0)
        self.assertEqual(len(workers), 1)
        self.assertEqual(workers[0].get("worker_id"), "worker-a")
        self.assertEqual(missing, [])


if __name__ == "__main__":
    unittest.main()
