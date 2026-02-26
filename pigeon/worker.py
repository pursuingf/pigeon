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
import threading
import time
from typing import Dict, List, Tuple

from .common import (
    DEFAULT_POLL_INTERVAL,
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
    read_json,
    request_path,
    status_path,
    stdin_path,
    stream_path,
    tail_jsonl,
    utc_iso,
)


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


def _discover_pending(config: PigeonConfig) -> List[str]:
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
        except Exception:
            continue
        if state == "pending":
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


def _run_session_once(config: PigeonConfig, session_id: str) -> int:
    req = read_json(request_path(config, session_id))
    command = req.get("command")
    cwd = req.get("cwd")
    if not isinstance(command, list) or not command:
        raise RuntimeError("invalid command in request")
    if not isinstance(cwd, str):
        raise RuntimeError("invalid cwd in request")

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
    else:
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
            if rec.get("type") != "signal":
                continue
            sig = rec.get("signal")
            if not isinstance(sig, int):
                continue
            try:
                os.killpg(proc.pid, sig)
            except ProcessLookupError:
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
                    append_jsonl(
                        stream_out,
                        {
                            "type": "output",
                            "seq": stdout_seq,
                            "ts": utc_iso(),
                            "channel": "stdout" if fd == stdout_fd else "stderr",
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


def _run_session(config: PigeonConfig, session_id: str) -> Tuple[int, str]:
    req = read_json(request_path(config, session_id))
    cwd = req.get("cwd")
    if not isinstance(cwd, str):
        raise RuntimeError("invalid cwd")
    lock = cwd_lock_path(config, cwd)
    with FileLock(lock):
        code = _run_session_once(config, session_id)
    if code == 0:
        _update_status(config, session_id, "succeeded", finished_at=utc_iso(), exit_code=0)
    else:
        _update_status(config, session_id, "failed", finished_at=utc_iso(), exit_code=code)
    return code, "ok"


def _run_session_safe(config: PigeonConfig, session_id: str) -> Tuple[int, str]:
    try:
        return _run_session(config, session_id)
    except Exception as exc:
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
    config = PigeonConfig.from_env()
    config.ensure_dirs()
    max_jobs = max(1, int(parsed_args.max_jobs))
    poll_interval = float(parsed_args.poll_interval)
    stop = threading.Event()

    def _stop(signum, frame) -> None:
        stop.set()

    old_int = signal.getsignal(signal.SIGINT)
    old_term = signal.getsignal(signal.SIGTERM)
    signal.signal(signal.SIGINT, _stop)
    signal.signal(signal.SIGTERM, _stop)

    futures: Dict[concurrent.futures.Future, str] = {}
    try:
        with concurrent.futures.ThreadPoolExecutor(max_workers=max_jobs) as pool:
            while not stop.is_set():
                done = [f for f in futures if f.done()]
                for f in done:
                    futures.pop(f, None)
                    try:
                        f.result()
                    except Exception:
                        pass

                capacity = max_jobs - len(futures)
                if capacity > 0:
                    for sid in _discover_pending(config):
                        if capacity <= 0:
                            break
                        if not _try_claim(config, sid):
                            continue
                        fut = pool.submit(_run_session_safe, config, sid)
                        futures[fut] = sid
                        capacity -= 1

                time.sleep(max(poll_interval, 0.01))
    finally:
        signal.signal(signal.SIGINT, old_int)
        signal.signal(signal.SIGTERM, old_term)
    return 0
