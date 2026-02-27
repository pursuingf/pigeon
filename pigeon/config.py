from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

ACTIVE_CONFIG_ENV = "PIGEON_CONFIG"
CONFIG_ROOT_ENV = "PIGEON_CONFIG_ROOT"
DEFAULT_CONFIG_FILENAME = "config.toml"
ACTIVE_CONFIG_POINTER_FILENAME = "active_config_path"
DEFAULT_BOOTSTRAP_CACHE = "/tmp/pigeon-cache"
DEFAULT_BOOTSTRAP_WORKER_MAX_JOBS = 4
DEFAULT_BOOTSTRAP_WORKER_POLL_INTERVAL = 0.05
DEFAULT_BOOTSTRAP_WORKER_DEBUG = False

_ENV_KEY_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

_BOOL_TRUE = {"1", "true", "yes", "on"}
_BOOL_FALSE = {"0", "false", "no", "off"}

_CONFIG_KEYS = (
    "cache",
    "namespace",
    "route",
    "user",
    "worker.max_jobs",
    "worker.poll_interval",
    "worker.debug",
    "worker.route",
    "remote_env.<NAME>",
)


@dataclass(frozen=True)
class FileConfig:
    path: Optional[Path]
    cache: Optional[str]
    namespace: Optional[str]
    route: Optional[str]
    user: Optional[str]
    worker_max_jobs: Optional[int]
    worker_poll_interval: Optional[float]
    worker_debug: Optional[bool]
    worker_route: Optional[str]
    remote_env: Dict[str, str]


def configurable_keys() -> tuple[str, ...]:
    return _CONFIG_KEYS


def config_root_dir() -> Path:
    raw = os.environ.get(CONFIG_ROOT_ENV)
    if raw:
        return Path(raw).expanduser().resolve()
    return (Path.home() / ".config" / "pigeon").resolve()


def _home_default_path() -> Path:
    return config_root_dir() / DEFAULT_CONFIG_FILENAME


def active_config_pointer_path() -> Path:
    return _home_default_path().parent / ACTIVE_CONFIG_POINTER_FILENAME


def get_active_config_path() -> Optional[Path]:
    pointer = active_config_pointer_path()
    if not pointer.exists():
        return None
    try:
        raw = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not raw:
        return None
    return Path(raw).expanduser().resolve()


def set_active_config_path(path: Path) -> Path:
    pointer = active_config_pointer_path()
    pointer.parent.mkdir(parents=True, exist_ok=True)
    target = path.expanduser().resolve()
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(pointer.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(str(target))
            tmp.write("\n")
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, pointer)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return target


def default_config_path() -> Path:
    by_env = os.environ.get(ACTIVE_CONFIG_ENV)
    if by_env:
        path = Path(by_env).expanduser().resolve()
        # Keep env-based selection and persisted active path aligned.
        current = get_active_config_path()
        if current != path:
            try:
                set_active_config_path(path)
            except OSError:
                pass
        return path
    active = get_active_config_path()
    if active is not None:
        return active
    return _home_default_path().expanduser().resolve()


def config_target_path(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    return default_config_path()


def discover_config_path(explicit: Optional[str]) -> Optional[Path]:
    target = config_target_path(explicit)
    if target.exists():
        return target
    return None


def _empty_file_config(path: Optional[Path]) -> FileConfig:
    return FileConfig(
        path=path,
        cache=None,
        namespace=None,
        route=None,
        user=None,
        worker_max_jobs=None,
        worker_poll_interval=None,
        worker_debug=None,
        worker_route=None,
        remote_env={},
    )


def _bootstrap_file_config(path: Path) -> FileConfig:
    user = (os.environ.get("PIGEON_USER") or os.environ.get("USER") or "").strip() or None
    namespace = (
        os.environ.get("PIGEON_NAMESPACE")
        or user
        or "default"
    ).strip()
    route = (os.environ.get("PIGEON_ROUTE") or "").strip() or None
    worker_route = (os.environ.get("PIGEON_WORKER_ROUTE") or "").strip() or route
    cache = (os.environ.get("PIGEON_CACHE") or DEFAULT_BOOTSTRAP_CACHE).strip()
    worker_max_jobs = _env_positive_int("PIGEON_WORKER_MAX_JOBS") or DEFAULT_BOOTSTRAP_WORKER_MAX_JOBS
    worker_poll_interval = (
        _env_positive_float("PIGEON_WORKER_POLL_INTERVAL") or DEFAULT_BOOTSTRAP_WORKER_POLL_INTERVAL
    )
    worker_debug = _env_bool("PIGEON_WORKER_DEBUG")
    if worker_debug is None:
        worker_debug = DEFAULT_BOOTSTRAP_WORKER_DEBUG

    return FileConfig(
        path=path,
        cache=cache,
        namespace=namespace,
        route=route,
        user=user,
        worker_max_jobs=worker_max_jobs,
        worker_poll_interval=worker_poll_interval,
        worker_debug=worker_debug,
        worker_route=worker_route,
        remote_env={},
    )


def _ensure_str(value: Any, field: str, path: Path) -> str:
    if not isinstance(value, str):
        raise RuntimeError(f"{path}: '{field}' must be string")
    return value


def _ensure_int(value: Any, field: str, path: Path) -> int:
    if not isinstance(value, int):
        raise RuntimeError(f"{path}: '{field}' must be integer")
    return int(value)


def _ensure_float(value: Any, field: str, path: Path) -> float:
    if isinstance(value, int):
        return float(value)
    if not isinstance(value, float):
        raise RuntimeError(f"{path}: '{field}' must be number")
    return float(value)


def _ensure_bool(value: Any, field: str, path: Path) -> bool:
    if not isinstance(value, bool):
        raise RuntimeError(f"{path}: '{field}' must be boolean")
    return bool(value)


def _parse_config(path: Path) -> FileConfig:
    try:
        with path.open("rb") as fh:
            raw = tomllib.load(fh)
    except FileNotFoundError:
        raise RuntimeError(f"config file not found: {path}") from None
    except tomllib.TOMLDecodeError as exc:
        raise RuntimeError(f"{path}: invalid TOML: {exc}") from exc

    if not isinstance(raw, dict):
        raise RuntimeError(f"{path}: top-level config must be table")

    cache: Optional[str] = None
    namespace: Optional[str] = None
    route: Optional[str] = None
    user: Optional[str] = None
    worker_max_jobs: Optional[int] = None
    worker_poll_interval: Optional[float] = None
    worker_debug: Optional[bool] = None
    worker_route: Optional[str] = None
    remote_env: Dict[str, str] = {}

    if "cache" in raw:
        cache = _ensure_str(raw["cache"], "cache", path)
    if "namespace" in raw:
        namespace = _ensure_str(raw["namespace"], "namespace", path)
    if "route" in raw:
        route = _ensure_str(raw["route"], "route", path)
    if "user" in raw:
        user = _ensure_str(raw["user"], "user", path)

    worker = raw.get("worker")
    if worker is not None:
        if not isinstance(worker, dict):
            raise RuntimeError(f"{path}: 'worker' must be table")
        if "max_jobs" in worker:
            worker_max_jobs = _ensure_int(worker["max_jobs"], "worker.max_jobs", path)
            if worker_max_jobs <= 0:
                raise RuntimeError(f"{path}: 'worker.max_jobs' must be > 0")
        if "poll_interval" in worker:
            worker_poll_interval = _ensure_float(worker["poll_interval"], "worker.poll_interval", path)
            if worker_poll_interval <= 0:
                raise RuntimeError(f"{path}: 'worker.poll_interval' must be > 0")
        if "debug" in worker:
            worker_debug = _ensure_bool(worker["debug"], "worker.debug", path)
        if "route" in worker:
            worker_route = _ensure_str(worker["route"], "worker.route", path)

    r_env = raw.get("remote_env")
    if r_env is not None:
        if not isinstance(r_env, dict):
            raise RuntimeError(f"{path}: 'remote_env' must be table")
        for k, v in r_env.items():
            key = _ensure_str(k, "remote_env key", path)
            val = _ensure_str(v, f"remote_env.{key}", path)
            remote_env[key] = val

    return FileConfig(
        path=path,
        cache=cache,
        namespace=namespace,
        route=route,
        user=user,
        worker_max_jobs=worker_max_jobs,
        worker_poll_interval=worker_poll_interval,
        worker_debug=worker_debug,
        worker_route=worker_route,
        remote_env=remote_env,
    )


def load_file_config(explicit: Optional[str]) -> FileConfig:
    target = config_target_path(explicit)
    if not target.exists():
        return _empty_file_config(target)
    return _parse_config(target)


def ensure_file_config(explicit: Optional[str]) -> Tuple[FileConfig, bool]:
    target = config_target_path(explicit)
    if target.exists():
        return _parse_config(target), False
    boot = _bootstrap_file_config(target)
    written = write_file_config(boot, explicit)
    return replace(boot, path=written), True


def _fill_missing_defaults(cfg: FileConfig, explicit: Optional[str]) -> FileConfig:
    target = cfg.path or config_target_path(explicit)
    base = _bootstrap_file_config(target)
    return replace(
        cfg,
        path=target,
        cache=cfg.cache if cfg.cache is not None else base.cache,
        namespace=cfg.namespace if cfg.namespace is not None else base.namespace,
        route=cfg.route if cfg.route is not None else base.route,
        user=cfg.user if cfg.user is not None else base.user,
        worker_max_jobs=cfg.worker_max_jobs if cfg.worker_max_jobs is not None else base.worker_max_jobs,
        worker_poll_interval=(
            cfg.worker_poll_interval if cfg.worker_poll_interval is not None else base.worker_poll_interval
        ),
        worker_debug=cfg.worker_debug if cfg.worker_debug is not None else base.worker_debug,
        worker_route=cfg.worker_route if cfg.worker_route is not None else base.worker_route,
        remote_env=dict(cfg.remote_env),
    )


def refresh_file_config(explicit: Optional[str]) -> Tuple[FileConfig, bool, bool]:
    cfg, created = ensure_file_config(explicit)
    refreshed = _fill_missing_defaults(cfg, explicit)
    changed = config_to_toml(cfg) != config_to_toml(refreshed)
    if created or changed:
        written = write_file_config(refreshed, explicit)
        refreshed = replace(refreshed, path=written)
    return refreshed, created, changed


def _env_non_empty(name: str) -> Optional[str]:
    raw = os.environ.get(name)
    if raw is None:
        return None
    val = raw.strip()
    if not val:
        return None
    return val


def _env_positive_int(name: str) -> Optional[int]:
    raw = _env_non_empty(name)
    if raw is None:
        return None
    try:
        out = int(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid integer env {name}: {raw!r}") from exc
    if out <= 0:
        raise RuntimeError(f"invalid integer env {name}: must be > 0")
    return out


def _env_positive_float(name: str) -> Optional[float]:
    raw = _env_non_empty(name)
    if raw is None:
        return None
    try:
        out = float(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid float env {name}: {raw!r}") from exc
    if out <= 0:
        raise RuntimeError(f"invalid float env {name}: must be > 0")
    return out


def _env_bool(name: str) -> Optional[bool]:
    raw = _env_non_empty(name)
    if raw is None:
        return None
    val = raw.lower()
    if val in _BOOL_TRUE:
        return True
    if val in _BOOL_FALSE:
        return False
    raise RuntimeError(f"invalid boolean env {name}: {raw!r}")


def sync_env_to_file_config(explicit: Optional[str]) -> Tuple[FileConfig, bool, bool]:
    cfg, created = ensure_file_config(explicit)
    updated = cfg
    cache = _env_non_empty("PIGEON_CACHE")
    namespace = _env_non_empty("PIGEON_NAMESPACE")
    user = _env_non_empty("PIGEON_USER")
    route = _env_non_empty("PIGEON_ROUTE")
    worker_route = _env_non_empty("PIGEON_WORKER_ROUTE")
    worker_max_jobs = _env_positive_int("PIGEON_WORKER_MAX_JOBS")
    worker_poll_interval = _env_positive_float("PIGEON_WORKER_POLL_INTERVAL")
    worker_debug = _env_bool("PIGEON_WORKER_DEBUG")

    if cache is not None:
        updated = replace(updated, cache=cache)
    if namespace is not None:
        updated = replace(updated, namespace=namespace)
    if user is not None:
        updated = replace(updated, user=user)
    if route is not None:
        updated = replace(updated, route=route)
    if worker_route is not None:
        updated = replace(updated, worker_route=worker_route)
    if worker_max_jobs is not None:
        updated = replace(updated, worker_max_jobs=worker_max_jobs)
    if worker_poll_interval is not None:
        updated = replace(updated, worker_poll_interval=worker_poll_interval)
    if worker_debug is not None:
        updated = replace(updated, worker_debug=worker_debug)

    changed = config_to_toml(cfg) != config_to_toml(updated)
    if created or changed:
        written = write_file_config(updated, explicit)
        updated = replace(updated, path=written)
    return updated, created, changed


def _parse_bool_literal(raw: str, key: str) -> bool:
    v = raw.strip().lower()
    if v in _BOOL_TRUE:
        return True
    if v in _BOOL_FALSE:
        return False
    raise RuntimeError(f"invalid boolean for {key}: {raw!r}")


def _non_empty(raw: str, key: str) -> str:
    v = raw.strip()
    if not v:
        raise RuntimeError(f"{key} cannot be empty")
    return v


def set_config_value(cfg: FileConfig, key: str, value: str) -> FileConfig:
    k = key.strip()
    if k == "cache":
        return replace(cfg, cache=_non_empty(value, k))
    if k == "namespace":
        return replace(cfg, namespace=_non_empty(value, k))
    if k == "route":
        return replace(cfg, route=_non_empty(value, k))
    if k == "user":
        return replace(cfg, user=_non_empty(value, k))
    if k == "worker.max_jobs":
        n = int(value)
        if n <= 0:
            raise RuntimeError("worker.max_jobs must be > 0")
        return replace(cfg, worker_max_jobs=n)
    if k == "worker.poll_interval":
        f = float(value)
        if f <= 0:
            raise RuntimeError("worker.poll_interval must be > 0")
        return replace(cfg, worker_poll_interval=f)
    if k == "worker.debug":
        return replace(cfg, worker_debug=_parse_bool_literal(value, k))
    if k == "worker.route":
        return replace(cfg, worker_route=_non_empty(value, k))
    if k.startswith("remote_env."):
        env_key = k[len("remote_env.") :].strip()
        if not _ENV_KEY_RE.match(env_key):
            raise RuntimeError("remote_env key must match [A-Za-z_][A-Za-z0-9_]*")
        env = dict(cfg.remote_env)
        env[env_key] = value
        return replace(cfg, remote_env=env)
    raise RuntimeError(f"unknown key: {key!r}")


def unset_config_value(cfg: FileConfig, key: str) -> FileConfig:
    k = key.strip()
    if k == "cache":
        return replace(cfg, cache=None)
    if k == "namespace":
        return replace(cfg, namespace=None)
    if k == "route":
        return replace(cfg, route=None)
    if k == "user":
        return replace(cfg, user=None)
    if k == "worker.max_jobs":
        return replace(cfg, worker_max_jobs=None)
    if k == "worker.poll_interval":
        return replace(cfg, worker_poll_interval=None)
    if k == "worker.debug":
        return replace(cfg, worker_debug=None)
    if k == "worker.route":
        return replace(cfg, worker_route=None)
    if k.startswith("remote_env."):
        env_key = k[len("remote_env.") :].strip()
        if not _ENV_KEY_RE.match(env_key):
            raise RuntimeError("remote_env key must match [A-Za-z_][A-Za-z0-9_]*")
        env = dict(cfg.remote_env)
        env.pop(env_key, None)
        return replace(cfg, remote_env=env)
    raise RuntimeError(f"unknown key: {key!r}")


def _q(v: str) -> str:
    return json.dumps(v, ensure_ascii=True)


def config_to_toml(cfg: FileConfig) -> str:
    lines: list[str] = []
    if cfg.cache is not None:
        lines.append(f"cache = {_q(cfg.cache)}")
    if cfg.namespace is not None:
        lines.append(f"namespace = {_q(cfg.namespace)}")
    if cfg.route is not None:
        lines.append(f"route = {_q(cfg.route)}")
    if cfg.user is not None:
        lines.append(f"user = {_q(cfg.user)}")

    has_worker = any(
        x is not None
        for x in (cfg.worker_max_jobs, cfg.worker_poll_interval, cfg.worker_debug, cfg.worker_route)
    )
    if has_worker:
        if lines:
            lines.append("")
        lines.append("[worker]")
        if cfg.worker_max_jobs is not None:
            lines.append(f"max_jobs = {cfg.worker_max_jobs}")
        if cfg.worker_poll_interval is not None:
            lines.append(f"poll_interval = {cfg.worker_poll_interval}")
        if cfg.worker_debug is not None:
            lines.append(f"debug = {'true' if cfg.worker_debug else 'false'}")
        if cfg.worker_route is not None:
            lines.append(f"route = {_q(cfg.worker_route)}")

    if cfg.remote_env:
        if lines:
            lines.append("")
        lines.append("[remote_env]")
        for k in sorted(cfg.remote_env):
            lines.append(f"{k} = {_q(cfg.remote_env[k])}")

    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def write_file_config(cfg: FileConfig, explicit: Optional[str] = None) -> Path:
    path = cfg.path or config_target_path(explicit)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = config_to_toml(cfg)
    fd, tmp_name = tempfile.mkstemp(prefix=".tmp-", dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as tmp:
            tmp.write(payload)
            tmp.flush()
            os.fsync(tmp.fileno())
        os.replace(tmp_name, path)
    finally:
        if os.path.exists(tmp_name):
            os.unlink(tmp_name)
    return path.resolve()
