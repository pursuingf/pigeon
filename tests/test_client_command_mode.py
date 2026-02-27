from __future__ import annotations

import os
import unittest
from unittest import mock

from pigeon.client import _build_request, _normalize_exec_command
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

    def test_plain_command_is_wrapped_with_bash_lc(self) -> None:
        wrapped = _normalize_exec_command(["codex", "--version"], self._cfg())
        self.assertEqual(wrapped[0:2], ["bash", "-lc"])
        self.assertEqual(wrapped[2], "codex --version")

    def test_shell_lc_command_is_kept(self) -> None:
        cmd = ["bash", "-lc", "echo hi"]
        wrapped = _normalize_exec_command(cmd, self._cfg())
        self.assertEqual(wrapped, cmd)

    def test_arguments_are_shell_escaped(self) -> None:
        wrapped = _normalize_exec_command(["python", "-c", 'print("a b")'], self._cfg())
        self.assertEqual(wrapped[0:2], ["bash", "-lc"])
        self.assertIn("python -c", wrapped[2])
        self.assertIn("'print(\"a b\")'", wrapped[2])

    def test_single_argument_shell_snippet_is_used_verbatim(self) -> None:
        wrapped = _normalize_exec_command(["pwd; echo hi"], self._cfg())
        self.assertEqual(wrapped, ["bash", "-lc", "pwd; echo hi"])

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
        requester = req["requester"]
        self.assertIsInstance(requester, dict)
        self.assertEqual(requester["user"], "cfg-user")
        self.assertIsNone(req["route"])

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
        self.assertEqual(wrapped[0:2], ["bash", "-lc"])
        self.assertEqual(wrapped[2], "echo $HTTPS_PROXY")

    def test_rewrite_prefers_inline_assignment_rhs(self) -> None:
        file_cfg = self._cfg({"HTTPS_PROXY": "http://proxy.example:8080"})
        with mock.patch.dict(os.environ, {"HTTPS_PROXY": "http://0.0.0.0:7890"}, clear=True):
            wrapped = _normalize_exec_command(
                ["HTTPS_PROXY=http://proxy.example:8080", "echo", "http://0.0.0.0:7890"],
                file_cfg,
            )
        self.assertEqual(wrapped[0:2], ["bash", "-lc"])
        self.assertEqual(wrapped[2], "HTTPS_PROXY=http://proxy.example:8080 echo http://proxy.example:8080")


if __name__ == "__main__":
    unittest.main()
