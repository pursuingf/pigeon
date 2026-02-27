from __future__ import annotations

import base64
import fcntl
import hashlib
import json
import os
import re
import socket
import tempfile
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from .config import FileConfig

DEFAULT_POLL_INTERVAL = 0.05
WORKER_HEARTBEAT_STALE_SECONDS = 3.0
WORKER_HEARTBEAT_INTERVAL_SECONDS = 1.0
_WORKER_ID_SAFE_RE = re.compile(r"[^A-Za-z0-9_.-]")


@dataclass(frozen=True)
class PigeonConfig:
    cache_root: Path
    namespace: str

    @classmethod
    def from_sources(cls, file_config: Optional[FileConfig] = None) -> "PigeonConfig":
        cache = os.environ.get("PIGEON_CACHE")
        if not cache and file_config is not None:
            cache = file_config.cache
        if not cache:
            raise RuntimeError("PIGEON_CACHE is required and must point to shared cache directory")
        ns = (
            os.environ.get("PIGEON_NAMESPACE")
            or (file_config.namespace if file_config is not None else None)
            or (file_config.user if file_config is not None else None)
            or os.environ.get("USER")
            or "default"
        )
        return cls(cache_root=Path(cache).expanduser().resolve(), namespace=ns)

    @property
    def ns_root(self) -> Path:
        return self.cache_root / "namespaces" / self.namespace

    @property
    def sessions_dir(self) -> Path:
        return self.ns_root / "sessions"

    @property
    def locks_dir(self) -> Path:
        return self.ns_root / "locks"

    @property
    def workers_dir(self) -> Path:
        return self.ns_root / "workers"

    def ensure_dirs(self) -> None:
        self.sessions_dir.mkdir(parents=True, exist_ok=True)
        self.locks_dir.mkdir(parents=True, exist_ok=True)
        self.workers_dir.mkdir(parents=True, exist_ok=True)


def now_ts() -> float:
    return time.time()


def utc_iso(ts: Optional[float] = None) -> str:
    if ts is None:
        ts = now_ts()
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime(ts)) + f".{int((ts % 1) * 1_000_000):06d}Z"


def new_session_id() -> str:
    return f"{int(now_ts() * 1000)}-{uuid.uuid4().hex[:12]}"


def host_name() -> str:
    return socket.gethostname()


def normalize_route(value: object) -> Optional[str]:
    if value is None:
        return None
    if not isinstance(value, str):
        return None
    route = value.strip()
    return route or None


def route_matches(worker_route: Optional[str], req_route: Optional[str]) -> bool:
    if req_route is None:
        return worker_route is None
    return worker_route == req_route


def session_dir(config: PigeonConfig, session_id: str) -> Path:
    return config.sessions_dir / session_id


def request_path(config: PigeonConfig, session_id: str) -> Path:
    return session_dir(config, session_id) / "request.json"


def status_path(config: PigeonConfig, session_id: str) -> Path:
    return session_dir(config, session_id) / "status.json"


def stream_path(config: PigeonConfig, session_id: str) -> Path:
    return session_dir(config, session_id) / "stream.jsonl"


def stdin_path(config: PigeonConfig, session_id: str) -> Path:
    return session_dir(config, session_id) / "stdin.jsonl"


def control_path(config: PigeonConfig, session_id: str) -> Path:
    return session_dir(config, session_id) / "control.jsonl"


def claim_path(config: PigeonConfig, session_id: str) -> Path:
    return session_dir(config, session_id) / "worker.claim"


def worker_heartbeat_path(config: PigeonConfig, worker_id: str) -> Path:
    safe = _WORKER_ID_SAFE_RE.sub("_", worker_id)
    return config.workers_dir / f"{safe}.json"


def write_worker_heartbeat(
    config: PigeonConfig,
    worker_id: str,
    *,
    route: Optional[str],
    host: str,
    pid: int,
    started_at: str,
    now: Optional[float] = None,
) -> Path:
    ts = now_ts() if now is None else float(now)
    payload = {
        "worker_id": worker_id,
        "host": host,
        "pid": int(pid),
        "route": normalize_route(route),
        "started_at": started_at,
        "updated_at": utc_iso(ts),
        "updated_ts": ts,
    }
    path = worker_heartbeat_path(config, worker_id)
    atomic_write_json(path, payload)
    return path


def remove_worker_heartbeat(config: PigeonConfig, worker_id: str) -> None:
    path = worker_heartbeat_path(config, worker_id)
    try:
        path.unlink()
    except FileNotFoundError:
        return


def discover_active_workers(
    config: PigeonConfig,
    req_route: Optional[str],
    *,
    now: Optional[float] = None,
    stale_after: float = WORKER_HEARTBEAT_STALE_SECONDS,
) -> List[Dict[str, Any]]:
    if stale_after <= 0:
        stale_after = WORKER_HEARTBEAT_STALE_SECONDS
    base_now = now_ts() if now is None else float(now)
    out: List[Dict[str, Any]] = []
    if not config.workers_dir.exists():
        return out
    route = normalize_route(req_route)
    for entry in sorted(config.workers_dir.iterdir()):
        if not entry.is_file():
            continue
        try:
            rec = read_json(entry)
        except Exception:
            continue
        worker_route = normalize_route(rec.get("route"))
        if not route_matches(worker_route, route):
            continue
        raw_ts = rec.get("updated_ts")
        if not isinstance(raw_ts, (int, float)):
            continue
        if base_now - float(raw_ts) > stale_after:
            continue
        out.append(rec)
    return out

def cwd_lock_path(config: PigeonConfig, cwd: str) -> Path:
    digest = hashlib.sha256(cwd.encode("utf-8")).hexdigest()
    return config.locks_dir / f"{digest}.lock"


def atomic_write_json(path: Path, data: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = json.dumps(data, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
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


def read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def append_jsonl(path: Path, record: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    line = json.dumps(record, ensure_ascii=True, separators=(",", ":"), sort_keys=True)
    with path.open("a", encoding="utf-8") as fh:
        fh.write(line)
        fh.write("\n")
        fh.flush()
        os.fsync(fh.fileno())


def tail_jsonl(path: Path, offset: int) -> Tuple[int, Iterator[Dict[str, Any]]]:
    if not path.exists():
        return offset, iter(())
    size = path.stat().st_size
    if offset > size:
        offset = 0
    with path.open("r", encoding="utf-8") as fh:
        fh.seek(offset)
        data = fh.read()
    if not data:
        return offset, iter(())

    # Only parse full lines. Keep a trailing partial JSON line unread to avoid
    # dropping records when reader races with writer appends.
    last_newline = data.rfind("\n")
    if last_newline < 0:
        return offset, iter(())
    parseable = data[: last_newline + 1]
    new_offset = offset + len(parseable)

    def _iter() -> Iterator[Dict[str, Any]]:
        for line in parseable.splitlines():
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                continue

    return new_offset, _iter()


def encode_bytes(data: bytes) -> str:
    return base64.b64encode(data).decode("ascii")


def decode_bytes(raw: str) -> bytes:
    return base64.b64decode(raw.encode("ascii"))


class FileLock:
    def __init__(self, path: Path):
        self.path = path
        self._fh = None

    def __enter__(self) -> "FileLock":
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._fh = self.path.open("a+")
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_EX)
        return self

    def __exit__(self, exc_type, exc, tb) -> None:
        if self._fh is None:
            return
        fcntl.flock(self._fh.fileno(), fcntl.LOCK_UN)
        self._fh.close()
        self._fh = None
