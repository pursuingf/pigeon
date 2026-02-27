from __future__ import annotations

import tempfile
import unittest
from pathlib import Path

from pigeon.common import (
    PigeonConfig,
    discover_active_workers,
    now_ts,
    route_matches,
    write_worker_heartbeat,
)
from pigeon.config import FileConfig
from pigeon.worker import (
    _downgrade_interactive_shell_flag,
    _normalize_route,
    _resolve_worker_debug,
    _resolve_worker_poll_interval,
    _resolve_worker_route,
    _route_matches,
)


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
        self.assertEqual(_route_matches("cpu-a", "cpu-a"), route_matches("cpu-a", "cpu-a"))

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

    def test_discover_active_workers_respects_route(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = PigeonConfig(cache_root=Path(tmp), namespace="ns")
            cfg.ensure_dirs()
            now = now_ts()
            write_worker_heartbeat(
                cfg,
                "worker-default",
                route=None,
                host="h",
                pid=1,
                started_at="2026-01-01T00:00:00.000000Z",
                now=now,
            )
            write_worker_heartbeat(
                cfg,
                "worker-a",
                route="cpu-a",
                host="h",
                pid=2,
                started_at="2026-01-01T00:00:00.000000Z",
                now=now,
            )
            default_workers = discover_active_workers(cfg, None, now=now, stale_after=3.0)
            route_workers = discover_active_workers(cfg, "cpu-a", now=now, stale_after=3.0)

        self.assertEqual(len(default_workers), 1)
        self.assertEqual(default_workers[0].get("worker_id"), "worker-default")
        self.assertEqual(len(route_workers), 1)
        self.assertEqual(route_workers[0].get("worker_id"), "worker-a")

    def test_discover_active_workers_ignores_stale(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = PigeonConfig(cache_root=Path(tmp), namespace="ns")
            cfg.ensure_dirs()
            now = now_ts()
            write_worker_heartbeat(
                cfg,
                "worker-stale",
                route=None,
                host="h",
                pid=1,
                started_at="2026-01-01T00:00:00.000000Z",
                now=now - 60.0,
            )
            active = discover_active_workers(cfg, None, now=now, stale_after=3.0)
        self.assertEqual(active, [])

    @staticmethod
    def _file_cfg(**kwargs) -> FileConfig:
        base = {
            "path": None,
            "cache": None,
            "namespace": None,
            "route": None,
            "user": None,
            "worker_max_jobs": None,
            "worker_poll_interval": None,
            "worker_debug": None,
            "worker_route": None,
            "remote_env": {},
        }
        base.update(kwargs)
        return FileConfig(**base)

    def test_worker_runtime_resolution_from_file(self) -> None:
        args = type("Args", (), {"route": None, "poll_interval": None, "debug": None})()
        cfg = self._file_cfg(route="cpu-file", worker_route="cpu-worker", worker_poll_interval=0.3, worker_debug=True)
        self.assertEqual(_resolve_worker_route(args, cfg), "cpu-worker")
        self.assertAlmostEqual(_resolve_worker_poll_interval(args, cfg), 0.3, places=6)
        self.assertTrue(_resolve_worker_debug(args, cfg))

    def test_worker_runtime_resolution_cli_has_priority(self) -> None:
        args = type("Args", (), {"route": "cpu-cli", "poll_interval": 0.6, "debug": False})()
        cfg = self._file_cfg(route="cpu-file", worker_route="cpu-worker", worker_poll_interval=0.3, worker_debug=True)
        self.assertEqual(_resolve_worker_route(args, cfg), "cpu-cli")
        self.assertAlmostEqual(_resolve_worker_poll_interval(args, cfg), 0.6, places=6)
        self.assertFalse(_resolve_worker_debug(args, cfg))


if __name__ == "__main__":
    unittest.main()
