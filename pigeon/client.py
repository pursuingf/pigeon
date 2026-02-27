from __future__ import annotations

import argparse
import os
import re
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
from .config import FileConfig, load_file_config


def _read_terminal_size() -> Optional[Dict[str, int]]:
    if not sys.stdin.isatty():
        return None
    try:
        cols, rows = os.get_terminal_size(sys.stdin.fileno())
    except OSError:
        return None
    return {"cols": int(cols), "rows": int(rows)}


def _resolve_request_user(file_config: FileConfig) -> str:
    return os.environ.get("PIGEON_USER") or file_config.user or os.environ.get("USER", "")


def _resolve_request_route(parsed_args: argparse.Namespace, file_config: FileConfig) -> Optional[str]:
    route = (
        getattr(parsed_args, "route", None)
        or os.environ.get("PIGEON_ROUTE")
        or file_config.route
    )
    if route is None:
        return None
    route = str(route).strip()
    return route or None


def _build_request(
    command: Sequence[str],
    cwd: str,
    session_id: str,
    file_config: FileConfig,
    route: Optional[str],
) -> Dict[str, object]:
    env = {k: v for k, v in os.environ.items() if isinstance(v, str)}
    # Remote env from config wins over local env and will also override worker-side
    # environment because request env is applied last in worker.
    env.update(file_config.remote_env)
    term_size = _read_terminal_size()
    return {
        "session_id": session_id,
        "command": list(command),
        "cwd": cwd,
        "route": route,
        "created_at": utc_iso(),
        "requester": {
            "host": host_name(),
            "pid": os.getpid(),
            "user": _resolve_request_user(file_config),
        },
        "env": env,
        "terminal": {
            "stdin_isatty": sys.stdin.isatty(),
            "stdout_isatty": sys.stdout.isatty(),
            "size": term_size,
        },
    }


def _is_shell_c(command: Sequence[str]) -> bool:
    if len(command) < 3:
        return False
    shell = str(command[0])
    if shell not in {"bash", "/bin/bash", "sh", "/bin/sh", "zsh", "/bin/zsh"}:
        return False
    flag = str(command[1])
    if not flag.startswith("-") or flag.startswith("--"):
        return False
    return "c" in flag[1:]


_ASSIGN_RE = re.compile(r"^([A-Za-z_][A-Za-z0-9_]*)=(.*)$")
_VAR_REF_RE = re.compile(r"^\$(?:[A-Za-z_][A-Za-z0-9_]*|\{[A-Za-z_][A-Za-z0-9_]*\})$")


def _prefix_assignments(command: Sequence[str]) -> Dict[str, str]:
    out: Dict[str, str] = {}
    for tok in command:
        m = _ASSIGN_RE.match(str(tok))
        if not m:
            break
        out[m.group(1)] = m.group(2)
    return out


def _shell_join_tokens(command: Sequence[str]) -> str:
    parts: List[str] = []
    for tok in command:
        s = str(tok)
        if _VAR_REF_RE.match(s):
            parts.append(s)
        else:
            parts.append(shlex.quote(s))
    return " ".join(parts)


def _rewrite_local_expanded_env_tokens(command: Sequence[str], file_config: FileConfig) -> List[str]:
    tokens = [str(x) for x in command]
    if not tokens:
        return tokens
    if not file_config.remote_env:
        return tokens

    local_env = {k: v for k, v in os.environ.items() if isinstance(v, str)}
    assignments = _prefix_assignments(tokens)
    assign_count = len(assignments)

    candidates = set(file_config.remote_env.keys()) | set(assignments.keys())
    if not candidates:
        return tokens

    for i in range(assign_count, len(tokens)):
        tok = tokens[i]
        replaced = tok
        for name in candidates:
            local_val = local_env.get(name)
            if not local_val or tok != local_val:
                continue
            if name in assignments:
                # Case: `VAR=new cmd $VAR` in caller shell where `$VAR` got
                # expanded early to local value; use assignment RHS as expected.
                replaced = assignments[name]
            elif name in file_config.remote_env:
                # Case: token came from early local expansion; restore remote ref.
                replaced = f"${name}"
            break
        tokens[i] = replaced
    return tokens


def _normalize_exec_command(
    command: Sequence[str],
    file_config: FileConfig,
    shell_flag: str,
) -> List[str]:
    if _is_shell_c(command):
        return list(command)
    if len(command) == 1:
        # A single argument can be an intentional shell snippet like:
        #   pigeon 'cd x && make'
        return ["bash", shell_flag, str(command[0])]
    rewritten = _rewrite_local_expanded_env_tokens(command, file_config)
    return ["bash", shell_flag, _shell_join_tokens(rewritten)]


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

    file_config = load_file_config(getattr(parsed_args, "config", None))
    config = PigeonConfig.from_sources(file_config)
    config.ensure_dirs()

    cwd = str(Path.cwd().resolve())
    session_id = new_session_id()
    sdir = session_dir(config, session_id)
    sdir.mkdir(parents=True, exist_ok=False)
    shell_flag = "-ic" if sys.stdin.isatty() and sys.stdout.isatty() else "-lc"

    request = _build_request(
        command=_normalize_exec_command(command, file_config, shell_flag=shell_flag),
        cwd=cwd,
        session_id=session_id,
        file_config=file_config,
        route=_resolve_request_route(parsed_args, file_config),
    )
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
