from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pigeon.common import PigeonConfig
from pigeon.config import (
    config_target_path,
    config_to_toml,
    default_config_path,
    discover_config_path,
    ensure_file_config,
    load_file_config,
    set_config_value,
    unset_config_value,
    write_file_config,
)


class ConfigTests(unittest.TestCase):
    def test_default_config_path_can_be_overridden_by_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "default.toml"
            with mock.patch.dict(os.environ, {"PIGEON_DEFAULT_CONFIG": str(p)}, clear=True):
                got = default_config_path()
        self.assertEqual(got, p.resolve())

    def test_config_target_path_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit.toml"
            by_env = Path(tmp) / "env.toml"
            by_default = Path(tmp) / "default.toml"
            with mock.patch.dict(
                os.environ,
                {"PIGEON_CONFIG": str(by_env), "PIGEON_DEFAULT_CONFIG": str(by_default)},
                clear=True,
            ):
                self.assertEqual(config_target_path(str(explicit)), explicit.resolve())
                self.assertEqual(config_target_path(None), by_env.resolve())

            with mock.patch.dict(os.environ, {"PIGEON_DEFAULT_CONFIG": str(by_default)}, clear=True):
                self.assertEqual(config_target_path(None), by_default.resolve())

    def test_discover_returns_none_if_target_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "default.toml"
            with mock.patch.dict(os.environ, {"PIGEON_DEFAULT_CONFIG": str(p)}, clear=True):
                found = discover_config_path(None)
        self.assertIsNone(found)

    def test_discover_ignores_cwd_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            default_cfg = Path(tmp) / "global.toml"
            local_cfg = Path(tmp) / ".pigeon.toml"
            local_cfg.write_text('cache = "/tmp/local-cwd"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"PIGEON_DEFAULT_CONFIG": str(default_cfg)}, clear=True):
                found = discover_config_path(None)
        self.assertIsNone(found)

    def test_load_file_config_parses_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pigeon.toml"
            path.write_text(
                "\n".join(
                    [
                        'cache = "/tmp/cache-x"',
                        'namespace = "ns-x"',
                        'route = "route-x"',
                        'user = "user-x"',
                        "",
                        "[worker]",
                        "max_jobs = 8",
                        "poll_interval = 0.15",
                        "debug = true",
                        'route = "worker-route-x"',
                        "",
                        "[remote_env]",
                        'A = "1"',
                        'B = "2"',
                    ]
                ),
                encoding="utf-8",
            )
            cfg = load_file_config(str(path))
        self.assertEqual(cfg.cache, "/tmp/cache-x")
        self.assertEqual(cfg.namespace, "ns-x")
        self.assertEqual(cfg.route, "route-x")
        self.assertEqual(cfg.user, "user-x")
        self.assertEqual(cfg.worker_max_jobs, 8)
        self.assertAlmostEqual(cfg.worker_poll_interval or 0.0, 0.15, places=6)
        self.assertEqual(cfg.worker_debug, True)
        self.assertEqual(cfg.worker_route, "worker-route-x")
        self.assertEqual(cfg.remote_env, {"A": "1", "B": "2"})

    def test_load_file_config_missing_returns_empty_with_target_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "not-exist.toml"
            cfg = load_file_config(str(p))
        self.assertEqual(cfg.path, p.resolve())
        self.assertIsNone(cfg.cache)
        self.assertEqual(cfg.remote_env, {})

    def test_common_config_precedence_env_over_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pigeon.toml"
            path.write_text('cache = "/tmp/cache-from-file"\nnamespace = "ns-file"\nuser = "u-file"\n', encoding="utf-8")
            file_cfg = load_file_config(str(path))
            with mock.patch.dict(
                os.environ,
                {
                    "PIGEON_CACHE": "/tmp/cache-from-env",
                    "PIGEON_NAMESPACE": "ns-env",
                    "USER": "u-env",
                },
                clear=True,
            ):
                cfg = PigeonConfig.from_sources(file_cfg)
        self.assertEqual(str(cfg.cache_root), "/tmp/cache-from-env")
        self.assertEqual(cfg.namespace, "ns-env")

    def test_common_config_fallback_to_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "pigeon.toml"
            path.write_text('cache = "/tmp/cache-from-file"\nnamespace = "ns-file"\n', encoding="utf-8")
            file_cfg = load_file_config(str(path))
            with mock.patch.dict(os.environ, {}, clear=True):
                cfg = PigeonConfig.from_sources(file_cfg)
        self.assertEqual(str(cfg.cache_root), "/tmp/cache-from-file")
        self.assertEqual(cfg.namespace, "ns-file")

    def test_set_unset_and_write_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "cfg.toml"
            cfg = load_file_config(str(p))
            cfg = set_config_value(cfg, "cache", "/tmp/cache-z")
            cfg = set_config_value(cfg, "worker.max_jobs", "7")
            cfg = set_config_value(cfg, "worker.debug", "true")
            cfg = set_config_value(cfg, "remote_env.HTTPS_PROXY", "http://proxy:8080")
            out_path = write_file_config(cfg)
            self.assertEqual(out_path, p.resolve())
            loaded = load_file_config(str(p))
        self.assertEqual(loaded.cache, "/tmp/cache-z")
        self.assertEqual(loaded.worker_max_jobs, 7)
        self.assertTrue(loaded.worker_debug)
        self.assertEqual(loaded.remote_env["HTTPS_PROXY"], "http://proxy:8080")
        loaded = unset_config_value(loaded, "worker.max_jobs")
        self.assertIsNone(loaded.worker_max_jobs)

    def test_config_to_toml_empty(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg = load_file_config(str(Path(tmp) / "x.toml"))
        self.assertEqual(config_to_toml(cfg), "")

    def test_ensure_file_config_creates_default_values(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cfg.toml"
            with mock.patch.dict(os.environ, {"USER": "alice"}, clear=True):
                cfg, created = ensure_file_config(str(target))
                loaded = load_file_config(str(target))
            self.assertTrue(target.exists())
        self.assertTrue(created)
        self.assertEqual(cfg.path, target.resolve())
        self.assertEqual(loaded.cache, "/tmp/pigeon-cache")
        self.assertEqual(loaded.namespace, "alice")
        self.assertEqual(loaded.user, "alice")
        self.assertEqual(loaded.worker_max_jobs, 4)
        self.assertAlmostEqual(loaded.worker_poll_interval or 0.0, 0.2, places=6)
        self.assertFalse(loaded.worker_debug)
        self.assertEqual(loaded.remote_env, {})

    def test_ensure_file_config_respects_env_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cfg.toml"
            with mock.patch.dict(
                os.environ,
                {
                    "PIGEON_CACHE": "/shared/pigeon-cache",
                    "PIGEON_NAMESPACE": "ns-env",
                    "PIGEON_USER": "u-env",
                    "PIGEON_ROUTE": "route-env",
                    "PIGEON_WORKER_ROUTE": "worker-route-env",
                },
                clear=True,
            ):
                _, created = ensure_file_config(str(target))
                loaded = load_file_config(str(target))
        self.assertTrue(created)
        self.assertEqual(loaded.cache, "/shared/pigeon-cache")
        self.assertEqual(loaded.namespace, "ns-env")
        self.assertEqual(loaded.user, "u-env")
        self.assertEqual(loaded.route, "route-env")
        self.assertEqual(loaded.worker_route, "worker-route-env")

    def test_ensure_file_config_does_not_overwrite_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cfg.toml"
            target.write_text('cache = "/from/file"\nnamespace = "ns-file"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"PIGEON_CACHE": "/from/env"}, clear=True):
                loaded, created = ensure_file_config(str(target))
        self.assertFalse(created)
        self.assertEqual(loaded.cache, "/from/file")
        self.assertEqual(loaded.namespace, "ns-file")


if __name__ == "__main__":
    unittest.main()
