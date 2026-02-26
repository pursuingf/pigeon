from __future__ import annotations

import unittest

from pigeon.client import _normalize_exec_command


class ClientCommandModeTests(unittest.TestCase):
    def test_plain_command_is_wrapped_with_bash_lc(self) -> None:
        wrapped = _normalize_exec_command(["codex", "--version"])
        self.assertEqual(wrapped[0:2], ["bash", "-lc"])
        self.assertEqual(wrapped[2], "codex --version")

    def test_shell_lc_command_is_kept(self) -> None:
        cmd = ["bash", "-lc", "echo hi"]
        wrapped = _normalize_exec_command(cmd)
        self.assertEqual(wrapped, cmd)

    def test_arguments_are_shell_escaped(self) -> None:
        wrapped = _normalize_exec_command(["python", "-c", 'print("a b")'])
        self.assertEqual(wrapped[0:2], ["bash", "-lc"])
        self.assertIn("python -c", wrapped[2])
        self.assertIn("'print(\"a b\")'", wrapped[2])

    def test_single_argument_shell_snippet_is_used_verbatim(self) -> None:
        wrapped = _normalize_exec_command(["pwd; echo hi"])
        self.assertEqual(wrapped, ["bash", "-lc", "pwd; echo hi"])


if __name__ == "__main__":
    unittest.main()
