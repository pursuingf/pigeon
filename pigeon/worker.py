from __future__ import annotations

import argparse
import concurrent.futures
import errno
import fcntl
import os
import pty
import select
import signal
import subprocess
import sys
import threading
import time
from typing import Dict, List, Tuple

from .common import (
    DEFAULT_POLL_INTERVAL,
    WORKER_HEARTBEAT_INTERVAL_SECONDS,
    FileLock,
    PigeonConfig,
    append_jsonl,
    atomic_write_json,
    claim_path,
    control_path,
    cwd_lock_path,
    decode_bytes,
    encode_bytes,
    host_name,
    normalize_route,
    now_ts,
    read_json,
    remove_worker_heartbeat,
    request_path,
    route_matches,
    status_path,
    stdin_path,
    stream_path,
    tail_jsonl,
    utc_iso,
    write_worker_heartbeat,
)
from .config import FileConfig, config_target_path, load_file_config, sync_env_to_file_config

WORKER_CONFIG_RELOAD_INTERVAL_SECONDS = 1.0


def _normalize_route(value: object) -> str | None:
    return normalize_route(value)


def _route_matches(worker_route: str | None, req_route: str | None) -> bool:
    return route_matches(worker_route, req_route)


def _resolve_worker_route(parsed_args: argparse.Namespace, file_config: FileConfig) -> str | None:
    return _normalize_route(
        getattr(parsed_args, "route", None)
        or file_config.worker_route
        or file_config.route
    )


def _resolve_worker_poll_interval(parsed_args: argparse.Namespace, file_config: FileConfig) -> float:
    if parsed_args.poll_interval is not None:
        return float(parsed_args.poll_interval)
    if file_config.worker_poll_interval is not None:
        return float(file_config.worker_poll_interval)
    return 0.05


def _resolve_worker_debug(parsed_args: argparse.Namespace, file_config: FileConfig) -> bool:
    if getattr(parsed_args, "debug", None) is not None:
        return bool(parsed_args.debug)
    if file_config.worker_debug is not None:
        return bool(file_config.worker_debug)
    return False


def _downgrade_interactive_shell_flag(command: List[str]) -> List[str]:
    if len(command) < 3:
        return command
    shell = str(command[0])
    if shell not in {"bash", "/bin/bash", "sh", "/bin/sh", "zsh", "/bin/zsh"}:
        return command
    flag = str(command[1])
    if not flag.startswith("-") or "c" not in flag[1:] or "i" not in flag[1:]:
        return command
    new_flag = "-" + "".join(ch for ch in flag[1:] if ch != "i")
    if "c" not in new_flag:
        return command
    out = list(command)
    out[1] = new_flag
    return out


def _supports_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    term = os.environ.get("TERM", "")
    if term.lower() == "dumb":
        return False
    return sys.stdout.isatty()


def _paint(text: str, color_code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\x1b[{color_code}m{text}\x1b[0m"


def _debug_log(enabled: bool, message: str, kind: str = "info") -> None:
    if not enabled:
        return
    use_color = _supports_color()
    ts = time.strftime("%H:%M:%S")
    kind_colors = {
        "lifecycle": "96",  # bright cyan
        "queue": "95",  # bright magenta
        "lock": "94",  # bright blue
        "stdin": "92",  # bright green
        "stdout": "37",  # white
        "stderr": "93",  # bright yellow
        "signal": "91",  # bright red
        "success": "92",  # bright green
        "failure": "91",  # bright red
        "error": "91",  # bright red
        "transport": "36",  # cyan
        "info": "90",  # bright black
    }
    kind_label = kind.upper()
    color = kind_colors.get(kind, kind_colors["info"])
    prefix = _paint("[pigeon-worker]", "90", use_color)
    debug_tag = _paint("[debug]", "2", use_color)
    kind_tag = _paint(f"[{kind_label}]", color, use_color)
    ts_tag = _paint(ts, "90", use_color)
    msg = _paint(message, color, use_color)
    print(f"{prefix}{debug_tag}{kind_tag} {ts_tag} {msg}", flush=True)


def _bytes_preview(data: bytes, limit: int = 96) -> str:
    cut = data[:limit]
    hex_part = " ".join(f"{b:02x}" for b in cut)
    txt = cut.decode("utf-8", "backslashreplace")
    txt = txt.replace("\n", "\\n").replace("\r", "\\r").replace("\t", "\\t")
    extra = ""
    if len(data) > limit:
        extra = f" ...(+{len(data) - limit}b)"
    return f"len={len(data)} hex=[{hex_part}] text='{txt}'{extra}"


def _format_command(req: Dict[str, object]) -> str:
    cmd = req.get("command")
    if not isinstance(cmd, list):
        return "<invalid>"
    return " ".join(str(x) for x in cmd)


def _shell_exit_code(returncode: int) -> int:
    if returncode >= 0:
        return returncode
    return 128 + abs(returncode)


def _set_winsize(fd: int, rows: int, cols: int) -> None:
    import struct
    import termios

    winsize = struct.pack("HHHH", rows, cols, 0, 0)
    fcntl.ioctl(fd, termios.TIOCSWINSZ, winsize)


def _update_status(config: PigeonConfig, session_id: str, state: str, **extra) -> None:
    path = status_path(config, session_id)
    prior: Dict[str, object] = {}
    if path.exists():
        prior = read_json(path)
    prior.update(extra)
    prior["session_id"] = session_id
    prior["state"] = state
    prior["updated_at"] = utc_iso()
    atomic_write_json(path, prior)


def _discover_pending(config: PigeonConfig, worker_route: str | None) -> List[str]:
    if not config.sessions_dir.exists():
        return []
    ids: List[str] = []
    for entry in sorted(config.sessions_dir.iterdir()):
        if not entry.is_dir():
            continue
        sid = entry.name
        req = request_path(config, sid)
        st = status_path(config, sid)
        if not req.exists() or not st.exists():
            continue
        try:
            state = read_json(st).get("state")
            req_route = _normalize_route(read_json(req).get("route"))
        except Exception:
            continue
        if state == "pending":
            if _route_matches(worker_route, req_route):
                ids.append(sid)
    return ids


def _try_claim(config: PigeonConfig, session_id: str) -> bool:
    path = claim_path(config, session_id)
    try:
        fd = os.open(path, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
    except FileExistsError:
        return False
    with os.fdopen(fd, "w", encoding="utf-8") as fh:
        fh.write(f"worker_host={host_name()}\n")
        fh.write(f"worker_pid={os.getpid()}\n")
        fh.flush()
        os.fsync(fh.fileno())
    return True


def _run_session_once(config: PigeonConfig, session_id: str, debug: bool = False) -> int:
    req = read_json(request_path(config, session_id))
    command = req.get("command")
    cwd = req.get("cwd")
    if not isinstance(command, list) or not command:
        raise RuntimeError("invalid command in request")
    if not isinstance(cwd, str):
        raise RuntimeError("invalid cwd in request")
    _debug_log(
        debug,
        f"session={session_id} exec begin route={_normalize_route(req.get('route')) or '-'} cwd={cwd} cmd={_format_command(req)}",
        kind="lifecycle",
    )

    terminal = req.get("terminal", {})
    if not isinstance(terminal, dict):
        terminal = {}
    size = terminal.get("size")
    if not isinstance(size, dict):
        size = None

    env_in = req.get("env")
    env: Dict[str, str] = dict(os.environ)
    if isinstance(env_in, dict):
        for k, v in env_in.items():
            if isinstance(k, str) and isinstance(v, str):
                env[k] = v

    _update_status(
        config,
        session_id,
        "running",
        started_at=utc_iso(),
        worker={"host": host_name(), "pid": os.getpid()},
        exit_code=None,
    )
    append_jsonl(stream_path(config, session_id), {"type": "event", "event": "started", "ts": utc_iso()})

    use_pty = True
    try:
        master_fd, slave_fd = pty.openpty()
    except OSError:
        use_pty = False
        master_fd = -1
        slave_fd = -1

    if use_pty:
        if size:
            rows = int(size.get("rows", 24))
            cols = int(size.get("cols", 80))
            _set_winsize(slave_fd, rows=max(rows, 1), cols=max(cols, 1))
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=slave_fd,
            stdout=slave_fd,
            stderr=slave_fd,
            env=env,
            start_new_session=True,
            close_fds=True,
        )
        os.close(slave_fd)
        _debug_log(debug, f"session={session_id} exec transport=pty", kind="transport")
    else:
        downgraded = _downgrade_interactive_shell_flag(command)
        if downgraded != command:
            _debug_log(
                debug,
                f"session={session_id} pty unavailable, shell flag downgraded: {command[1]} -> {downgraded[1]}",
                kind="transport",
            )
            command = downgraded
        append_jsonl(
            stream_path(config, session_id),
            {"type": "event", "event": "pty_fallback_to_pipes", "ts": utc_iso()},
        )
        proc = subprocess.Popen(
            command,
            cwd=cwd,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            env=env,
            start_new_session=True,
            close_fds=True,
            bufsize=0,
        )
        _debug_log(
            debug,
            f"session={session_id} exec transport=pipes (pty unavailable)",
            kind="transport",
        )

    stdout_seq = 0
    in_offset = 0
    ctrl_offset = 0
    stdin_eof_forwarded = False
    stream_out = stream_path(config, session_id)
    in_file = stdin_path(config, session_id)
    ctrl_file = control_path(config, session_id)
    stdout_fd = proc.stdout.fileno() if proc.stdout else -1
    stderr_fd = proc.stderr.fileno() if proc.stderr else -1
    stdout_open = stdout_fd >= 0
    stderr_open = stderr_fd >= 0
    pty_open = use_pty

    while True:
        in_offset, in_records = tail_jsonl(in_file, in_offset)
        for rec in in_records:
            typ = rec.get("type")
            if typ == "stdin":
                raw = rec.get("data_b64")
                if isinstance(raw, str):
                    payload = decode_bytes(raw)
                    _debug_log(
                        debug,
                        f"session={session_id} stdin seq={rec.get('seq')} {_bytes_preview(payload)}",
                        kind="stdin",
                    )
                    if use_pty:
                        try:
                            os.write(master_fd, payload)
                        except OSError:
                            pass
                    else:
                        if proc.stdin:
                            try:
                                proc.stdin.write(payload)
                                proc.stdin.flush()
                            except (BrokenPipeError, OSError):
                                pass
            elif typ == "stdin_eof":
                if stdin_eof_forwarded:
                    continue
                _debug_log(debug, f"session={session_id} stdin eof", kind="stdin")
                if use_pty:
                    try:
                        os.write(master_fd, b"\x04")
                    except OSError:
                        pass
                else:
                    if proc.stdin:
                        try:
                            proc.stdin.close()
                        except OSError:
                            pass
                stdin_eof_forwarded = True

        ctrl_offset, ctrl_records = tail_jsonl(ctrl_file, ctrl_offset)
        for rec in ctrl_records:
            typ = rec.get("type")
            if typ == "signal":
                sig = rec.get("signal")
                if not isinstance(sig, int):
                    continue
                try:
                    os.killpg(proc.pid, sig)
                    _debug_log(debug, f"session={session_id} signal forwarded sig={sig}", kind="signal")
                except ProcessLookupError:
                    pass
            elif typ == "resize" and use_pty:
                cols = rec.get("cols")
                rows = rec.get("rows")
                if not isinstance(cols, int) or not isinstance(rows, int):
                    continue
                cols = max(cols, 1)
                rows = max(rows, 1)
                try:
                    _set_winsize(master_fd, rows=rows, cols=cols)
                    _debug_log(
                        debug,
                        f"session={session_id} resize applied cols={cols} rows={rows}",
                        kind="transport",
                    )
                except OSError:
                    pass

        if use_pty:
            read_fds = [master_fd] if pty_open else []
        else:
            read_fds = []
            if stdout_open:
                read_fds.append(stdout_fd)
            if stderr_open:
                read_fds.append(stderr_fd)
        ready, _, _ = select.select(read_fds, [], [], DEFAULT_POLL_INTERVAL)
        for fd in ready:
            if use_pty:
                try:
                    chunk = os.read(master_fd, 4096)
                except OSError as exc:
                    if exc.errno == errno.EIO:
                        chunk = b""
                    else:
                        raise
                if chunk:
                    _debug_log(
                        debug,
                        f"session={session_id} output channel=pty {_bytes_preview(chunk)}",
                        kind="stdout",
                    )
                    append_jsonl(
                        stream_out,
                        {
                            "type": "output",
                            "seq": stdout_seq,
                            "ts": utc_iso(),
                            "channel": "pty",
                            "data_b64": encode_bytes(chunk),
                        },
                    )
                    stdout_seq += 1
                else:
                    pty_open = False
            else:
                try:
                    chunk = os.read(fd, 4096)
                except OSError:
                    chunk = b""
                if chunk:
                    channel = "stdout" if fd == stdout_fd else "stderr"
                    _debug_log(
                        debug,
                        f"session={session_id} output channel={channel} {_bytes_preview(chunk)}",
                        kind="stdout" if channel == "stdout" else "stderr",
                    )
                    append_jsonl(
                        stream_out,
                        {
                            "type": "output",
                            "seq": stdout_seq,
                            "ts": utc_iso(),
                            "channel": channel,
                            "data_b64": encode_bytes(chunk),
                        },
                    )
                    stdout_seq += 1
                else:
                    if fd == stdout_fd:
                        stdout_open = False
                    if fd == stderr_fd:
                        stderr_open = False

        if use_pty:
            done = proc.poll() is not None
            if done and not pty_open:
                break
        else:
            if proc.poll() is not None and not stdout_open and not stderr_open:
                break

    code = proc.wait()
    if use_pty:
        try:
            os.close(master_fd)
        except OSError:
            pass
    shell_code = _shell_exit_code(int(code))
    _debug_log(
        debug,
        f"session={session_id} exec end raw_return={int(code)} shell_exit={shell_code}",
        kind="success" if shell_code == 0 else "failure",
    )
    append_jsonl(
        stream_out,
        {
            "type": "event",
            "event": "exit",
            "exit_code": shell_code,
            "raw_return_code": int(code),
            "ts": utc_iso(),
        },
    )
    return shell_code


def _run_session(config: PigeonConfig, session_id: str, debug: bool = False) -> Tuple[int, str]:
    req = read_json(request_path(config, session_id))
    cwd = req.get("cwd")
    if not isinstance(cwd, str):
        raise RuntimeError("invalid cwd")
    lock = cwd_lock_path(config, cwd)
    _debug_log(debug, f"session={session_id} waiting cwd_lock={lock}", kind="lock")
    with FileLock(lock):
        _debug_log(debug, f"session={session_id} acquired cwd_lock={lock}", kind="lock")
        code = _run_session_once(config, session_id, debug=debug)
    if code == 0:
        _update_status(config, session_id, "succeeded", finished_at=utc_iso(), exit_code=0)
    else:
        _update_status(config, session_id, "failed", finished_at=utc_iso(), exit_code=code)
    return code, "ok"


def _run_session_safe(config: PigeonConfig, session_id: str, debug: bool = False) -> Tuple[int, str]:
    try:
        return _run_session(config, session_id, debug=debug)
    except Exception as exc:
        _debug_log(debug, f"session={session_id} worker_error {type(exc).__name__}: {exc}", kind="error")
        append_jsonl(
            stream_path(config, session_id),
            {
                "type": "event",
                "event": "worker_error",
                "message": f"{type(exc).__name__}: {exc}",
                "ts": utc_iso(),
            },
        )
        _update_status(
            config,
            session_id,
            "failed",
            finished_at=utc_iso(),
            exit_code=1,
            error=f"{type(exc).__name__}: {exc}",
        )
        return 1, "error"


def run_worker(parsed_args: argparse.Namespace) -> int:
    file_config, created, _ = sync_env_to_file_config(None)
    if created:
        print(f"[pigeon-worker] initialized config: {file_config.path}", file=sys.stderr)
    config = PigeonConfig.from_sources(file_config)
    config.ensure_dirs()
    config_file_path = str(file_config.path) if file_config.path is not None else None
    if parsed_args.max_jobs is not None:
        max_jobs = max(1, int(parsed_args.max_jobs))
    elif file_config.worker_max_jobs is not None:
        max_jobs = max(1, int(file_config.worker_max_jobs))
    else:
        max_jobs = 4

    poll_interval = _resolve_worker_poll_interval(parsed_args, file_config)
    debug = _resolve_worker_debug(parsed_args, file_config)
    worker_route = _resolve_worker_route(parsed_args, file_config)
    worker_host = host_name()
    worker_pid = os.getpid()
    worker_id = f"{worker_host}-{worker_pid}"
    worker_started_at = utc_iso()
    next_heartbeat = 0.0
    next_config_reload = 0.0
    last_reload_error = ""

    def _heartbeat(force: bool = False) -> None:
        nonlocal next_heartbeat
        now = now_ts()
        if not force and now < next_heartbeat:
            return
        write_worker_heartbeat(
            config,
            worker_id,
            route=worker_route,
            host=worker_host,
            pid=worker_pid,
            started_at=worker_started_at,
            now=now,
        )
        next_heartbeat = now + max(WORKER_HEARTBEAT_INTERVAL_SECONDS, poll_interval)

    stop = threading.Event()

    def _stop(signum, frame) -> None:
        stop.set()

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    futures: Dict[concurrent.futures.Future, str] = {}
    try:
        _heartbeat(force=True)
        _debug_log(
            debug,
            f"worker start host={host_name()} pid={os.getpid()} namespace={config.namespace} cache={config.cache_root} route={worker_route or '-'}",
            kind="lifecycle",
        )
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_jobs) as pool:
            while not stop.is_set():
                now = now_ts()
                if config_file_path and now >= next_config_reload:
                    try:
                        latest_path = str(config_target_path(None))
                        if latest_path != config_file_path:
                            config_file_path = latest_path
                        fresh_cfg = load_file_config(config_file_path)
                        new_poll = _resolve_worker_poll_interval(parsed_args, fresh_cfg)
                        new_debug = _resolve_worker_debug(parsed_args, fresh_cfg)
                        new_route = _resolve_worker_route(parsed_args, fresh_cfg)
                        old_poll = poll_interval
                        old_debug = debug
                        old_route = worker_route
                        poll_interval = new_poll
                        debug = new_debug
                        worker_route = new_route
                        if old_poll != new_poll or old_debug != new_debug or old_route != new_route:
                            _debug_log(
                                old_debug or new_debug,
                                (
                                    f"config reloaded route={old_route or '-'}->{new_route or '-'} "
                                    f"poll={old_poll}->{new_poll} debug={old_debug}->{new_debug}"
                                ),
                                kind="lifecycle",
                            )
                            _heartbeat(force=True)
                        last_reload_error = ""
                    except Exception as exc:
                        msg = f"config reload failed: {type(exc).__name__}: {exc}"
                        if msg != last_reload_error:
                            print(f"[pigeon-worker] {msg}", file=sys.stderr, flush=True)
                            last_reload_error = msg
                    next_config_reload = now + WORKER_CONFIG_RELOAD_INTERVAL_SECONDS

                _heartbeat()
                done = [f for f in futures if f.done()]
                for f in done:
                    sid = futures.pop(f, None)
                    try:
                        code, _ = f.result()
                        _debug_log(
                            debug,
                            f"session={sid} completed exit={code}",
                            kind="success" if code == 0 else "failure",
                        )
                    except Exception:
                        _debug_log(
                            debug,
                            f"session={sid} completed with internal worker exception",
                            kind="error",
                        )

                capacity = max_jobs - len(futures)
                if capacity > 0:
                    for sid in _discover_pending(config, worker_route):
                        if capacity <= 0:
                            break
                        if not _try_claim(config, sid):
                            continue
                        _debug_log(debug, f"session={sid} claimed", kind="queue")
                        fut = pool.submit(_run_session_safe, config, sid, debug)
                        futures[fut] = sid
                        capacity -= 1

                time.sleep(max(poll_interval, 0.01))
    finally:
        remove_worker_heartbeat(config, worker_id)
        _debug_log(debug, "worker stop", kind="lifecycle")
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    return 0
