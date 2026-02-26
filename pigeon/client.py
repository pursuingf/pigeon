from __future__ import annotations

import argparse
import os
import shlex
import signal
import sys
import termios
import threading
import time
from pathlib import Path
from typing import Dict, List, Optional, Sequence

from .common import (
    DEFAULT_POLL_INTERVAL,
    PigeonConfig,
    append_jsonl,
    atomic_write_json,
    control_path,
    decode_bytes,
    encode_bytes,
    host_name,
    new_session_id,
    read_json,
    request_path,
    session_dir,
    status_path,
    stdin_path,
    stream_path,
    tail_jsonl,
    utc_iso,
)


def _read_terminal_size() -> Optional[Dict[str, int]]:
    if not sys.stdin.isatty():
        return None
    try:
        cols, rows = os.get_terminal_size(sys.stdin.fileno())
    except OSError:
        return None
    return {"cols": int(cols), "rows": int(rows)}


def _build_request(command: Sequence[str], cwd: str, session_id: str) -> Dict[str, object]:
    env = {k: v for k, v in os.environ.items() if isinstance(v, str)}
    term_size = _read_terminal_size()
    return {
        "session_id": session_id,
        "command": list(command),
        "cwd": cwd,
        "created_at": utc_iso(),
        "requester": {
            "host": host_name(),
            "pid": os.getpid(),
            "user": os.environ.get("USER", ""),
        },
        "env": env,
        "terminal": {
            "stdin_isatty": sys.stdin.isatty(),
            "stdout_isatty": sys.stdout.isatty(),
            "size": term_size,
        },
    }


def _is_shell_lc(command: Sequence[str]) -> bool:
    if len(command) < 3:
        return False
    shell = str(command[0])
    return shell in {"bash", "/bin/bash", "sh", "/bin/sh", "zsh", "/bin/zsh"} and str(command[1]) == "-lc"


def _normalize_exec_command(command: Sequence[str]) -> List[str]:
    if _is_shell_lc(command):
        return list(command)
    if len(command) == 1:
        # A single argument can be an intentional shell snippet like:
        #   pigeon 'cd x && make'
        return ["bash", "-lc", str(command[0])]
    return ["bash", "-lc", shlex.join([str(x) for x in command])]


class _TerminalMode:
    def __init__(self) -> None:
        self._fd: Optional[int] = None
        self._old_attrs = None

    def enter(self) -> None:
        if not sys.stdin.isatty():
            return
        self._fd = sys.stdin.fileno()
        self._old_attrs = termios.tcgetattr(self._fd)
        attrs = termios.tcgetattr(self._fd)
        # Use a near-raw proxy mode so interactive TUI keystrokes are forwarded
        # with minimal local line-discipline rewriting (notably Enter/CR).
        attrs[0] = attrs[0] & ~termios.BRKINT
        attrs[0] = attrs[0] & ~termios.ICRNL
        attrs[0] = attrs[0] & ~termios.INPCK
        attrs[0] = attrs[0] & ~termios.ISTRIP
        attrs[0] = attrs[0] & ~termios.IXON
        attrs[1] = attrs[1] & ~termios.OPOST
        attrs[2] = attrs[2] | termios.CS8
        attrs[3] = attrs[3] & ~termios.ECHO
        attrs[3] = attrs[3] & ~termios.ICANON
        attrs[3] = attrs[3] & ~termios.IEXTEN
        attrs[6][termios.VMIN] = 1
        attrs[6][termios.VTIME] = 0
        termios.tcsetattr(self._fd, termios.TCSAFLUSH, attrs)

    def exit(self) -> None:
        if self._fd is None or self._old_attrs is None:
            return
        termios.tcsetattr(self._fd, termios.TCSADRAIN, self._old_attrs)
        self._fd = None
        self._old_attrs = None


def _stdin_pump(config: PigeonConfig, session_id: str, stop: threading.Event) -> None:
    in_path = stdin_path(config, session_id)
    seq = 0
    fd = sys.stdin.fileno()
    while not stop.is_set():
        try:
            chunk = os.read(fd, 1024)
        except OSError:
            time.sleep(DEFAULT_POLL_INTERVAL)
            continue
        if not chunk:
            append_jsonl(
                in_path,
                {"type": "stdin_eof", "seq": seq, "ts": utc_iso()},
            )
            break
        append_jsonl(
            in_path,
            {
                "type": "stdin",
                "seq": seq,
                "ts": utc_iso(),
                "data_b64": encode_bytes(chunk),
            },
        )
        seq += 1


def run_command(command: List[str], parsed_args: argparse.Namespace) -> int:
    if not command:
        print("usage: pigeon <cmd...>", file=sys.stderr)
        return 2

    config = PigeonConfig.from_env()
    config.ensure_dirs()

    cwd = str(Path.cwd().resolve())
    session_id = new_session_id()
    sdir = session_dir(config, session_id)
    sdir.mkdir(parents=True, exist_ok=False)

    request = _build_request(command=_normalize_exec_command(command), cwd=cwd, session_id=session_id)
    atomic_write_json(request_path(config, session_id), request)
    atomic_write_json(
        status_path(config, session_id),
        {
            "session_id": session_id,
            "state": "pending",
            "created_at": request["created_at"],
            "updated_at": utc_iso(),
            "exit_code": None,
        },
    )

    stream_path(config, session_id).touch(exist_ok=True)
    stdin_path(config, session_id).touch(exist_ok=True)
    control_path(config, session_id).touch(exist_ok=True)

    terminal_mode = _TerminalMode()
    terminal_mode.enter()

    stop = threading.Event()
    stdin_thread = threading.Thread(target=_stdin_pump, args=(config, session_id, stop), daemon=True)
    if sys.stdin and hasattr(sys.stdin, "fileno"):
        stdin_thread.start()

    control_seq = {"value": 0}

    def _on_sigint(signum, frame) -> None:
        append_jsonl(
            control_path(config, session_id),
            {
                "type": "signal",
                "seq": control_seq["value"],
                "signal": int(signum),
                "ts": utc_iso(),
            },
        )
        control_seq["value"] += 1

    def _on_sigwinch(signum, frame) -> None:
        size = _read_terminal_size()
        if not size:
            return
        append_jsonl(
            control_path(config, session_id),
            {
                "type": "resize",
                "seq": control_seq["value"],
                "cols": size["cols"],
                "rows": size["rows"],
                "ts": utc_iso(),
            },
        )
        control_seq["value"] += 1

    old_sigint = signal.getsignal(signal.SIGINT)
    old_sigwinch = signal.getsignal(signal.SIGWINCH)
    signal.signal(signal.SIGINT, _on_sigint)
    signal.signal(signal.SIGWINCH, _on_sigwinch)

    stream_offset = 0
    last_state = "pending"
    exit_code = 1
    try:
        while True:
            stream_offset, records = tail_jsonl(stream_path(config, session_id), stream_offset)
            for rec in records:
                if rec.get("type") == "output":
                    data = rec.get("data_b64")
                    if isinstance(data, str):
                        out = decode_bytes(data)
                        channel = rec.get("channel")
                        if channel == "stderr":
                            sys.stderr.buffer.write(out)
                            sys.stderr.buffer.flush()
                        else:
                            sys.stdout.buffer.write(out)
                            sys.stdout.buffer.flush()
                elif rec.get("type") == "event" and rec.get("event") == "exit":
                    maybe_code = rec.get("exit_code")
                    if isinstance(maybe_code, int):
                        exit_code = maybe_code

            status = read_json(status_path(config, session_id))
            state = status.get("state", "unknown")
            maybe_code = status.get("exit_code")
            if isinstance(maybe_code, int):
                exit_code = maybe_code

            if state != last_state and parsed_args.verbose:
                print(f"\n[pigeon] session={session_id} state={state}", file=sys.stderr)
                last_state = state

            if state in {"succeeded", "failed", "cancelled"}:
                for _ in range(3):
                    time.sleep(DEFAULT_POLL_INTERVAL)
                    stream_offset, records = tail_jsonl(stream_path(config, session_id), stream_offset)
                    drained = False
                    for rec in records:
                        drained = True
                        if rec.get("type") == "output":
                            data = rec.get("data_b64")
                            if isinstance(data, str):
                                out = decode_bytes(data)
                                channel = rec.get("channel")
                                if channel == "stderr":
                                    sys.stderr.buffer.write(out)
                                    sys.stderr.buffer.flush()
                                else:
                                    sys.stdout.buffer.write(out)
                                    sys.stdout.buffer.flush()
                    if not drained:
                        break
                break

            time.sleep(DEFAULT_POLL_INTERVAL)
    finally:
        stop.set()
        terminal_mode.exit()
        signal.signal(signal.SIGINT, old_sigint)
        signal.signal(signal.SIGWINCH, old_sigwinch)
    return exit_code
