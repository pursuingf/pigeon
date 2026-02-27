from __future__ import annotations

import json
import os
import re
import tempfile
import tomllib
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Any, Dict, Optional, Tuple

DEFAULT_CONFIG_PATH = Path.home() / ".config" / "pigeon" / "config.toml"
DEFAULT_CONFIG_ENV = "PIGEON_DEFAULT_CONFIG"
DEFAULT_BOOTSTRAP_CACHE = "/tmp/pigeon-cache"
DEFAULT_BOOTSTRAP_WORKER_MAX_JOBS = 4
DEFAULT_BOOTSTRAP_WORKER_POLL_INTERVAL = 0.2
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


def default_config_path() -> Path:
    by_env = os.environ.get(DEFAULT_CONFIG_ENV)
    raw = by_env if by_env else str(DEFAULT_CONFIG_PATH)
    return Path(raw).expanduser().resolve()


def config_target_path(explicit: Optional[str]) -> Path:
    if explicit:
        return Path(explicit).expanduser().resolve()
    by_env = os.environ.get("PIGEON_CONFIG")
    if by_env:
        return Path(by_env).expanduser().resolve()
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

    return FileConfig(
        path=path,
        cache=cache,
        namespace=namespace,
        route=route,
        user=user,
        worker_max_jobs=DEFAULT_BOOTSTRAP_WORKER_MAX_JOBS,
        worker_poll_interval=DEFAULT_BOOTSTRAP_WORKER_POLL_INTERVAL,
        worker_debug=DEFAULT_BOOTSTRAP_WORKER_DEBUG,
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
