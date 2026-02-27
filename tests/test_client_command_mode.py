from __future__ import annotations

import argparse
import os
import tempfile
import unittest
from contextlib import redirect_stderr
from io import StringIO
from pathlib import Path
from unittest import mock

from pigeon.client import (
    _build_request,
    _find_ambiguous_operator_token,
    _format_interactive_panel,
    _normalize_exec_command,
    _resolve_worker_wait_timeout,
    _shell_prelude,
    _wait_for_worker,
    run_command,
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
            interactive_command=None,
            interactive_source_bashrc=None,
            remote_env=remote_env or {},
        )

    def test_plain_command_is_wrapped_with_clean_bash_c(self) -> None:
        wrapped = _normalize_exec_command(["codex", "--version"], self._cfg())
        self.assertEqual(wrapped[0:4], ["bash", "--noprofile", "--norc", "-c"])
        self.assertEqual(wrapped[4], "codex --version")

    def test_shell_snippet_mode_uses_single_argument(self) -> None:
        wrapped = _normalize_exec_command(["echo a | wc -c"], self._cfg(), command_mode="shell_snippet")
        self.assertEqual(wrapped[0:4], ["bash", "--noprofile", "--norc", "-c"])
        self.assertEqual(wrapped[4], "echo a | wc -c")

    def test_interactive_mode_uses_default_command(self) -> None:
        wrapped = _normalize_exec_command([], self._cfg(), command_mode="interactive")
        self.assertEqual(wrapped, ["bash", "--noprofile", "--norc", "-i"])

    def test_interactive_mode_uses_configured_command(self) -> None:
        cfg = FileConfig(**{**self._cfg().__dict__, "interactive_command": "zsh -i"})
        wrapped = _normalize_exec_command([], cfg, command_mode="interactive")
        self.assertEqual(wrapped, ["zsh", "-i"])

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
                prelude = _shell_prelude(self._cfg())
        self.assertIn("alias ls='ls --color=auto'\n", prelude)
        self.assertTrue(prelude.endswith("\n"))

    def test_shell_prelude_can_silently_source_bashrc(self) -> None:
        cfg = FileConfig(**{**self._cfg().__dict__, "interactive_source_bashrc": True})
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("pigeon.client.sys.stdout.isatty", return_value=False):
                prelude = _shell_prelude(cfg)
        self.assertIn(". ~/.bashrc >/dev/null 2>&1", prelude)

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

    def test_ambiguous_operator_token_detection(self) -> None:
        self.assertEqual(_find_ambiguous_operator_token(["echo", "|", "wc"]), "|")
        self.assertEqual(_find_ambiguous_operator_token(["echo", "&&", "true"]), "&&")
        self.assertEqual(_find_ambiguous_operator_token(["echo", "value"]), None)

    def test_run_command_rejects_ambiguous_operator_tokens(self) -> None:
        err = StringIO()
        args = argparse.Namespace(verbose=False, route=None, wait_worker=0.0)
        with redirect_stderr(err):
            rc = run_command(["echo", "|", "wc"], args, command_mode="argv")
        self.assertEqual(rc, 2)
        self.assertIn("ambiguous shell operator", err.getvalue())
        self.assertIn("pigeon -c", err.getvalue())

    def test_format_interactive_panel_contains_key_fields(self) -> None:
        cfg = FileConfig(
            **{
                **self._cfg({"HTTPS_PROXY": "http://proxy:8080"}).__dict__,
                "path": Path("/tmp/pigeon.toml"),
                "route": "cpu-a",
                "worker_route": "cpu-a",
                "worker_max_jobs": 8,
                "worker_poll_interval": 0.2,
                "worker_debug": True,
                "interactive_command": "bash --noprofile --norc -i",
                "interactive_source_bashrc": False,
            }
        )
        p_cfg = PigeonConfig(cache_root=Path("/tmp/pigeon-cache"), namespace="ns-a")
        with mock.patch.dict(os.environ, {}, clear=True):
            with mock.patch("pigeon.client.sys.stderr.isatty", return_value=False):
                out = _format_interactive_panel(
                    session_id="sid-a",
                    config=p_cfg,
                    cwd="/work/repo",
                    req_route="cpu-a",
                    file_config=cfg,
                    active_workers=[
                        {
                            "worker_id": "w1",
                            "host": "cpu-a",
                            "pid": 1234,
                            "route": "cpu-a",
                            "updated_at": "2026-02-27T00:00:00.000000Z",
                        },
                        {
                            "worker_id": "w2",
                            "host": "cpu-a",
                            "pid": 1235,
                            "route": "cpu-a",
                            "updated_at": "2026-02-27T00:00:01.000000Z",
                        },
                    ],
                    remote_command=["bash", "--noprofile", "--norc", "-i"],
                )
        self.assertIn("Pigeon Interactive", out)
        self.assertIn("session_id", out)
        self.assertIn("config.path", out)
        self.assertIn("route(request)", out)
        self.assertIn("Active Workers", out)
        self.assertIn("w1 host=cpu-a pid=1234 route=cpu-a", out)
        self.assertIn("remote.exec", out)
        self.assertIn("HTTPS_PROXY=http://proxy:8080", out)

    def test_format_interactive_panel_color_toggle(self) -> None:
        cfg = self._cfg()
        p_cfg = PigeonConfig(cache_root=Path("/tmp/pigeon-cache"), namespace="ns-a")
        with mock.patch.dict(os.environ, {"FORCE_COLOR": "1"}, clear=True):
            with mock.patch("pigeon.client.sys.stderr.isatty", return_value=False):
                colored = _format_interactive_panel(
                    session_id="sid-c",
                    config=p_cfg,
                    cwd="/tmp",
                    req_route=None,
                    file_config=cfg,
                    active_workers=[{"worker_id": "w1"}],
                    remote_command=["bash", "--noprofile", "--norc", "-i"],
                )
        self.assertIn("\x1b[", colored)
        with mock.patch.dict(os.environ, {"NO_COLOR": "1"}, clear=True):
            with mock.patch("pigeon.client.sys.stderr.isatty", return_value=True):
                plain = _format_interactive_panel(
                    session_id="sid-p",
                    config=p_cfg,
                    cwd="/tmp",
                    req_route=None,
                    file_config=cfg,
                    active_workers=[{"worker_id": "w1"}],
                    remote_command=["bash", "--noprofile", "--norc", "-i"],
                )
        self.assertNotIn("\x1b[", plain)

    def test_run_command_interactive_prints_panel_once(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / "cfg.toml"
            cache_root = Path(tmp) / "cache"
            env = {
                "PIGEON_CONFIG": str(cfg_path),
                "PIGEON_CACHE": str(cache_root),
                "PIGEON_NAMESPACE": "ns-test",
            }
            args = argparse.Namespace(verbose=False, route=None, wait_worker=0.0)
            with mock.patch.dict(os.environ, env, clear=True):
                with mock.patch("pigeon.client._wait_for_worker", return_value=[{"worker_id": "w1"}]):
                    with mock.patch("pigeon.client.discover_active_workers", return_value=[]):
                        with mock.patch("pigeon.client._print_interactive_panel") as panel_mock:
                            rc = run_command([], args, command_mode="interactive")
        self.assertEqual(rc, 4)
        panel_mock.assert_called_once()

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
