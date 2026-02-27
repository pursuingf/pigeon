from __future__ import annotations

import os
import tomllib
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Optional

DEFAULT_CONFIG_PATH = Path("/home/pgroup/pxd-team/workspace/fyh/pigeon/.pigeon.toml")


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


def discover_config_path(explicit: Optional[str]) -> Optional[Path]:
    if explicit:
        return Path(explicit).expanduser().resolve()
    by_env = os.environ.get("PIGEON_CONFIG")
    if by_env:
        return Path(by_env).expanduser().resolve()
    if DEFAULT_CONFIG_PATH.exists():
        return DEFAULT_CONFIG_PATH.expanduser().resolve()
    # Backward-compatible local fallback.
    cwd = Path.cwd()
    for name in (".pigeon.toml", "pigeon.toml"):
        candidate = cwd / name
        if candidate.exists():
            return candidate.resolve()
    return None


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
    path = discover_config_path(explicit)
    if path is None:
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
            remote_env={},
        )
    return _parse_config(path)
