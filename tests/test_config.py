from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pigeon.common import PigeonConfig
from pigeon.config import discover_config_path, load_file_config


class ConfigTests(unittest.TestCase):
    def test_discover_uses_default_path_when_no_flag_or_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            cfg_path = Path(tmp) / ".pigeon.toml"
            cfg_path.write_text('cache = "/tmp/cache-x"\n', encoding="utf-8")
            with mock.patch("pigeon.config.DEFAULT_CONFIG_PATH", cfg_path):
                with mock.patch.dict(os.environ, {}, clear=True):
                    found = discover_config_path(None)
        self.assertEqual(found, cfg_path.resolve())

    def test_discover_env_overrides_default_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            default_cfg = Path(tmp) / ".pigeon.toml"
            env_cfg = Path(tmp) / "env.toml"
            default_cfg.write_text('cache = "/tmp/cache-default"\n', encoding="utf-8")
            env_cfg.write_text('cache = "/tmp/cache-env"\n', encoding="utf-8")
            with mock.patch("pigeon.config.DEFAULT_CONFIG_PATH", default_cfg):
                with mock.patch.dict(os.environ, {"PIGEON_CONFIG": str(env_cfg)}, clear=True):
                    found = discover_config_path(None)
        self.assertEqual(found, env_cfg.resolve())

    def test_discover_explicit_overrides_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explicit_cfg = Path(tmp) / "explicit.toml"
            env_cfg = Path(tmp) / "env.toml"
            explicit_cfg.write_text('cache = "/tmp/cache-explicit"\n', encoding="utf-8")
            env_cfg.write_text('cache = "/tmp/cache-env"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"PIGEON_CONFIG": str(env_cfg)}, clear=True):
                found = discover_config_path(str(explicit_cfg))
        self.assertEqual(found, explicit_cfg.resolve())

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


if __name__ == "__main__":
    unittest.main()
