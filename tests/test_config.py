from __future__ import annotations

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from pigeon.common import PigeonConfig
from pigeon.config import (
    active_config_pointer_path,
    config_target_path,
    config_to_toml,
    default_config_path,
    discover_config_path,
    ensure_file_config,
    get_active_config_path,
    load_file_config,
    refresh_file_config,
    set_active_config_path,
    set_config_value,
    sync_env_to_file_config,
    unset_config_value,
    write_file_config,
)


class ConfigTests(unittest.TestCase):
    def test_default_config_path_can_be_overridden_by_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "env.toml"
            with mock.patch.dict(os.environ, {"HOME": tmp, "PIGEON_CONFIG": str(p)}, clear=True):
                got = default_config_path()
        self.assertEqual(got, p.resolve())

    def test_default_config_path_uses_config_root_env(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp) / "shared-root"
            with mock.patch.dict(
                os.environ,
                {"HOME": tmp, "PIGEON_CONFIG_ROOT": str(root)},
                clear=True,
            ):
                got = default_config_path()
        self.assertEqual(got, (root / "config.toml").resolve())

    def test_default_config_path_can_use_active_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active_target = Path(tmp) / "active.toml"
            active_target.write_text('cache = "/tmp/x"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": tmp}, clear=True):
                written = set_active_config_path(active_target)
                self.assertEqual(written, active_target.resolve())
                got_active = get_active_config_path()
                got_default = default_config_path()
                pointer = active_config_pointer_path()
                self.assertTrue(pointer.exists())
        self.assertEqual(got_active, active_target.resolve())
        self.assertEqual(got_default, active_target.resolve())

    def test_default_config_env_has_priority_over_active_pointer(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            active_target = Path(tmp) / "active.toml"
            explicit_target = Path(tmp) / "explicit.toml"
            active_target.write_text('cache = "/tmp/a"\n', encoding="utf-8")
            explicit_target.write_text('cache = "/tmp/b"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": tmp}, clear=True):
                set_active_config_path(active_target)
            with mock.patch.dict(
                os.environ,
                {"HOME": tmp, "PIGEON_CONFIG": str(explicit_target)},
                clear=True,
            ):
                got = default_config_path()
        self.assertEqual(got, explicit_target.resolve())

    def test_config_target_path_priority(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            explicit = Path(tmp) / "explicit.toml"
            by_env = Path(tmp) / "env.toml"
            by_active = Path(tmp) / "active.toml"
            by_default = Path(tmp) / ".config" / "pigeon" / "config.toml"
            by_default.parent.mkdir(parents=True, exist_ok=True)
            with mock.patch.dict(
                os.environ,
                {"HOME": tmp, "PIGEON_CONFIG": str(by_env)},
                clear=True,
            ):
                self.assertEqual(config_target_path(str(explicit)), explicit.resolve())
                self.assertEqual(config_target_path(None), by_env.resolve())

            with mock.patch.dict(os.environ, {"HOME": tmp}, clear=True):
                set_active_config_path(by_active)
                self.assertEqual(config_target_path(None), by_active.resolve())

            with mock.patch.dict(os.environ, {"HOME": tmp}, clear=True):
                pointer = active_config_pointer_path()
                if pointer.exists():
                    pointer.unlink()
                self.assertEqual(config_target_path(None), by_default.resolve())

    def test_discover_returns_none_if_target_does_not_exist(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            p = Path(tmp) / "default.toml"
            with mock.patch.dict(os.environ, {"PIGEON_CONFIG": str(p)}, clear=True):
                found = discover_config_path(None)
        self.assertIsNone(found)

    def test_discover_ignores_cwd_local_config(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            local_cfg = Path(tmp) / ".pigeon.toml"
            local_cfg.write_text('cache = "/tmp/local-cwd"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"HOME": tmp}, clear=True):
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
                        "[interactive]",
                        'command = "bash --noprofile --norc -i"',
                        "source_bashrc = true",
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
        self.assertEqual(cfg.interactive_command, "bash --noprofile --norc -i")
        self.assertTrue(cfg.interactive_source_bashrc)
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
        self.assertIsNone(cfg.interactive_command)
        self.assertIsNone(cfg.interactive_source_bashrc)
        self.assertEqual(cfg.remote_env, {})

    def test_common_config_precedence_file_over_env(self) -> None:
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
        self.assertEqual(str(cfg.cache_root), "/tmp/cache-from-file")
        self.assertEqual(cfg.namespace, "ns-file")

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
            cfg = set_config_value(cfg, "interactive.command", "bash -l -i")
            cfg = set_config_value(cfg, "interactive.source_bashrc", "true")
            cfg = set_config_value(cfg, "worker.max_jobs", "7")
            cfg = set_config_value(cfg, "worker.debug", "true")
            cfg = set_config_value(cfg, "remote_env.HTTPS_PROXY", "http://proxy:8080")
            out_path = write_file_config(cfg)
            self.assertEqual(out_path, p.resolve())
            loaded = load_file_config(str(p))
        self.assertEqual(loaded.cache, "/tmp/cache-z")
        self.assertEqual(loaded.interactive_command, "bash -l -i")
        self.assertTrue(loaded.interactive_source_bashrc)
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
        self.assertAlmostEqual(loaded.worker_poll_interval or 0.0, 0.05, places=6)
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

    def test_ensure_file_config_respects_interactive_env_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cfg.toml"
            with mock.patch.dict(
                os.environ,
                {
                    "PIGEON_INTERACTIVE_COMMAND": "bash -l -i",
                    "PIGEON_INTERACTIVE_SOURCE_BASHRC": "true",
                },
                clear=True,
            ):
                _, created = ensure_file_config(str(target))
                loaded = load_file_config(str(target))
        self.assertTrue(created)
        self.assertEqual(loaded.interactive_command, "bash -l -i")
        self.assertTrue(loaded.interactive_source_bashrc)

    def test_ensure_file_config_respects_worker_env_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cfg.toml"
            with mock.patch.dict(
                os.environ,
                {
                    "PIGEON_WORKER_MAX_JOBS": "9",
                    "PIGEON_WORKER_POLL_INTERVAL": "0.35",
                    "PIGEON_WORKER_DEBUG": "true",
                },
                clear=True,
            ):
                _, created = ensure_file_config(str(target))
                loaded = load_file_config(str(target))
        self.assertTrue(created)
        self.assertEqual(loaded.worker_max_jobs, 9)
        self.assertAlmostEqual(loaded.worker_poll_interval or 0.0, 0.35, places=6)
        self.assertTrue(loaded.worker_debug)

    def test_ensure_file_config_does_not_overwrite_existing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cfg.toml"
            target.write_text('cache = "/from/file"\nnamespace = "ns-file"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"PIGEON_CACHE": "/from/env"}, clear=True):
                loaded, created = ensure_file_config(str(target))
        self.assertFalse(created)
        self.assertEqual(loaded.cache, "/from/file")
        self.assertEqual(loaded.namespace, "ns-file")

    def test_refresh_file_config_fills_missing_defaults(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cfg.toml"
            target.write_text('cache = "/from-file"\n', encoding="utf-8")
            with mock.patch.dict(os.environ, {"USER": "refresh-user"}, clear=True):
                refreshed, created, changed = refresh_file_config(str(target))
            loaded = load_file_config(str(target))
        self.assertFalse(created)
        self.assertTrue(changed)
        self.assertEqual(refreshed.path, target.resolve())
        self.assertEqual(loaded.cache, "/from-file")
        self.assertEqual(loaded.namespace, "refresh-user")
        self.assertEqual(loaded.user, "refresh-user")
        self.assertEqual(loaded.interactive_command, "bash --noprofile --norc -i")
        self.assertFalse(loaded.interactive_source_bashrc)
        self.assertEqual(loaded.worker_max_jobs, 4)
        self.assertAlmostEqual(loaded.worker_poll_interval or 0.0, 0.05, places=6)
        self.assertFalse(loaded.worker_debug)

    def test_sync_env_to_file_config_updates_worker_fields(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            target = Path(tmp) / "cfg.toml"
            target.write_text(
                "\n".join(
                    [
                        'cache = "/tmp/cache-a"',
                        'namespace = "ns-a"',
                        "",
                        "[worker]",
                        "max_jobs = 2",
                        "poll_interval = 0.1",
                        "debug = false",
                    ]
                )
                + "\n",
                encoding="utf-8",
            )
            with mock.patch.dict(
                os.environ,
                {
                    "PIGEON_WORKER_MAX_JOBS": "11",
                    "PIGEON_WORKER_POLL_INTERVAL": "0.45",
                    "PIGEON_WORKER_DEBUG": "on",
                    "PIGEON_INTERACTIVE_COMMAND": "bash -l -i",
                    "PIGEON_INTERACTIVE_SOURCE_BASHRC": "yes",
                },
                clear=True,
            ):
                updated, created, changed = sync_env_to_file_config(str(target))
                loaded = load_file_config(str(target))
        self.assertFalse(created)
        self.assertTrue(changed)
        self.assertEqual(updated.interactive_command, "bash -l -i")
        self.assertTrue(updated.interactive_source_bashrc)
        self.assertEqual(updated.worker_max_jobs, 11)
        self.assertAlmostEqual(updated.worker_poll_interval or 0.0, 0.45, places=6)
        self.assertTrue(updated.worker_debug)
        self.assertEqual(loaded.interactive_command, "bash -l -i")
        self.assertTrue(loaded.interactive_source_bashrc)
        self.assertEqual(loaded.worker_max_jobs, 11)
        self.assertAlmostEqual(loaded.worker_poll_interval or 0.0, 0.45, places=6)
        self.assertTrue(loaded.worker_debug)


if __name__ == "__main__":
    unittest.main()
