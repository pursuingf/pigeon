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
    discover_active_workers,
    encode_bytes,
    host_name,
    new_session_id,
    now_ts,
    read_json,
    request_path,
    session_dir,
    status_path,
    stdin_path,
    stream_path,
    tail_jsonl,
    utc_iso,
)
from .config import FileConfig, sync_env_to_file_config

DEFAULT_WORKER_WAIT_SECONDS = 3.0
DEFAULT_INTERACTIVE_COMMAND = "bash --noprofile --norc -i"
DEFAULT_INTERACTIVE_PS1 = "[pigeon][\\u@\\h \\w]\\$ "
FORBIDDEN_ARGV_OPERATOR_TOKENS = {"|", "||", ";", "&&", "&", ">", ">>", "<", "<<", "(", ")"}


def _read_terminal_size() -> Optional[Dict[str, int]]:
    if not sys.stdin.isatty():
        return None
    try:
        cols, rows = os.get_terminal_size(sys.stdin.fileno())
    except OSError:
        return None
    return {"cols": int(cols), "rows": int(rows)}


def _resolve_request_user(file_config: FileConfig) -> str:
    return file_config.user or os.environ.get("USER", "")


def _resolve_request_route(parsed_args: argparse.Namespace, file_config: FileConfig) -> Optional[str]:
    route = getattr(parsed_args, "route", None) or file_config.route
    if route is None:
        return None
    route = str(route).strip()
    return route or None


def _resolve_worker_wait_timeout(parsed_args: argparse.Namespace) -> float:
    raw: object = getattr(parsed_args, "wait_worker", None)
    if raw is None:
        env_val = os.environ.get("PIGEON_WAIT_WORKER")
        raw = DEFAULT_WORKER_WAIT_SECONDS if env_val is None else env_val
    try:
        timeout = float(raw)
    except (TypeError, ValueError):
        timeout = DEFAULT_WORKER_WAIT_SECONDS
    if timeout < 0:
        return 0.0
    return timeout


def _wait_for_worker(config: PigeonConfig, route: Optional[str], timeout: float) -> List[Dict[str, object]]:
    deadline = now_ts() + max(timeout, 0.0)
    while True:
        workers = discover_active_workers(config, route)
        if workers:
            return workers
        if now_ts() >= deadline:
            return []
        remaining = max(0.0, deadline - now_ts())
        time.sleep(min(DEFAULT_POLL_INTERVAL, max(0.01, remaining)))


def _build_request(
    command: Sequence[str],
    cwd: str,
    session_id: str,
    file_config: FileConfig,
    route: Optional[str],
    extra_env: Optional[Dict[str, str]] = None,
    unset_env: Optional[Sequence[str]] = None,
) -> Dict[str, object]:
    # Do not forward caller(gpu_m) environment into remote execution.
    # Worker(cpu_m) process environment is the base, while config remote_env
    # explicitly overrides selected keys when needed.
    env = dict(extra_env or {})
    env.update(file_config.remote_env)
    term_size = _read_terminal_size()
    request: Dict[str, object] = {
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
    if unset_env:
        request["unset_env"] = [k for k in unset_env if isinstance(k, str) and k]
    return request


def _is_shell_c(command: Sequence[str]) -> bool:
    if len(command) < 2:
        return False
    shell = str(command[0])
    if shell not in {"bash", "/bin/bash", "sh", "/bin/sh", "zsh", "/bin/zsh"}:
        return False
    for tok in command[1:]:
        flag = str(tok)
        if flag == "-c":
            return True
        if flag.startswith("-") and not flag.startswith("--") and "c" in flag[1:]:
            return True
    return False


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
    command_mode: str = "argv",
) -> List[str]:
    if command_mode == "interactive":
        return _build_interactive_exec_command(file_config)

    shell_prefix = ["bash", "--noprofile", "--norc", "-c"]
    prelude = _shell_prelude(file_config)
    if command_mode == "shell_snippet":
        if len(command) != 1:
            raise RuntimeError("shell_snippet mode requires a single snippet argument")
        return [*shell_prefix, f"{prelude}{str(command[0])}"]

    if _is_shell_c(command):
        return list(command)
    if len(command) == 1:
        # A single argument can be an intentional shell snippet like:
        #   pigeon 'cd x && make'
        return [*shell_prefix, f"{prelude}{str(command[0])}"]
    rewritten = _rewrite_local_expanded_env_tokens(command, file_config)
    return [*shell_prefix, f"{prelude}{_shell_join_tokens(rewritten)}"]


def _build_interactive_exec_command(file_config: FileConfig) -> List[str]:
    raw = (file_config.interactive_command or DEFAULT_INTERACTIVE_COMMAND).strip()
    if not raw:
        raw = DEFAULT_INTERACTIVE_COMMAND
    try:
        parts = shlex.split(raw)
    except ValueError as exc:
        raise RuntimeError(f"invalid interactive.command: {exc}") from exc
    if not parts:
        raise RuntimeError("invalid interactive.command: empty command")
    if not _source_bashrc_enabled(file_config):
        return parts
    quoted = " ".join(shlex.quote(p) for p in parts)
    prelude = "if [ -r ~/.bashrc ]; then . ~/.bashrc >/dev/null 2>&1 || true; fi"
    return ["bash", "--noprofile", "--norc", "-c", f"{prelude}\nexec {quoted}"]


def _interactive_extra_env() -> Dict[str, str]:
    raw = os.environ.get("PIGEON_INTERACTIVE_PS1")
    prompt = DEFAULT_INTERACTIVE_PS1 if raw is None else str(raw)
    return {"PS1": prompt}


def _terminal_env_patch() -> tuple[Dict[str, str], List[str]]:
    copy_keys = (
        "TERM",
        "COLORTERM",
        "TERM_PROGRAM",
        "TERM_PROGRAM_VERSION",
        "LANG",
        "LC_ALL",
        "LC_CTYPE",
        "LS_COLORS",
        "NO_COLOR",
        "FORCE_COLOR",
    )
    out: Dict[str, str] = {}
    unset: List[str] = []
    for key in copy_keys:
        val = os.environ.get(key)
        if isinstance(val, str):
            out[key] = val
    # Keep color behavior aligned with the caller terminal: if the caller did
    # not request NO_COLOR/FORCE_COLOR, explicitly clear inherited worker-side
    # values from service launch context.
    if "NO_COLOR" not in out:
        unset.append("NO_COLOR")
    if "FORCE_COLOR" not in out:
        unset.append("FORCE_COLOR")
    return out, unset


def _shell_prelude(file_config: FileConfig) -> str:
    lines: List[str] = []
    if _source_bashrc_enabled(file_config):
        # Optional: load cpu-side ~/.bashrc without leaking any startup output.
        lines.append("if [ -r ~/.bashrc ]; then . ~/.bashrc >/dev/null 2>&1 || true; fi")
    if not os.environ.get("NO_COLOR") and sys.stdout.isatty():
        # Keep shell startup clean (no user rc/profile), but preserve common
        # interactive color behavior for basic tools.
        lines.extend(
            [
                "shopt -s expand_aliases",
                "alias ls='ls --color=always'",
                "alias grep='grep --color=auto'",
                "alias egrep='egrep --color=auto'",
                "alias fgrep='fgrep --color=auto'",
            ]
        )
    if not lines:
        return ""
    return "\n".join(lines) + "\n"


def _source_bashrc_enabled(file_config: FileConfig) -> bool:
    if file_config.interactive_source_bashrc is not None:
        return bool(file_config.interactive_source_bashrc)
    raw = (
        os.environ.get("PIGEON_INTERACTIVE_SOURCE_BASHRC")
        or os.environ.get("PIGEON_SOURCE_BASHRC")
        or ""
    ).strip().lower()
    return raw in {"1", "true", "yes", "on"}


def _find_ambiguous_operator_token(command: Sequence[str]) -> Optional[str]:
    for tok in command:
        token = str(tok)
        if token in FORBIDDEN_ARGV_OPERATOR_TOKENS:
            return token
    return None


def _supports_client_color() -> bool:
    if os.environ.get("NO_COLOR"):
        return False
    if os.environ.get("FORCE_COLOR"):
        return True
    term = os.environ.get("TERM", "")
    if term.lower() == "dumb":
        return False
    return sys.stderr.isatty()


def _paint_client(text: str, color_code: str, enabled: bool) -> str:
    if not enabled:
        return text
    return f"\x1b[{color_code}m{text}\x1b[0m"


def _format_remote_env(remote_env: Dict[str, str]) -> str:
    if not remote_env:
        return "<none>"
    pairs = [f"{k}={v}" for k, v in sorted(remote_env.items())]
    return ", ".join(pairs)


def _format_active_workers(active_workers: Sequence[Dict[str, object]]) -> List[str]:
    if not active_workers:
        return ["<none>"]
    lines: List[str] = []
    preview_limit = 3
    for rec in active_workers[:preview_limit]:
        worker_id = str(rec.get("worker_id") or "<unknown>")
        host = str(rec.get("host") or "<unknown>")
        pid = rec.get("pid")
        route = rec.get("route")
        updated = rec.get("updated_at")
        pid_text = str(pid) if isinstance(pid, int) else "-"
        route_text = str(route).strip() if isinstance(route, str) and str(route).strip() else "-"
        updated_text = str(updated) if isinstance(updated, str) and updated else "-"
        lines.append(
            f"{worker_id} host={host} pid={pid_text} route={route_text} heartbeat={updated_text}"
        )
    if len(active_workers) > preview_limit:
        lines.append(f"... +{len(active_workers) - preview_limit} more")
    return lines


def _format_exec_preview(command: Sequence[str]) -> str:
    return " ".join(shlex.quote(str(tok)) for tok in command) if command else "<empty>"


def _format_interactive_panel(
    *,
    session_id: str,
    config: PigeonConfig,
    cwd: str,
    req_route: Optional[str],
    file_config: FileConfig,
    active_workers: Sequence[Dict[str, object]],
    remote_command: Sequence[str],
) -> str:
    color = _supports_client_color()
    title = _paint_client("Pigeon Interactive Session", "96;1", color)
    line = _paint_client("=" * 96, "90", color)
    section = lambda s: _paint_client(f"[{s}]", "95;1", color)
    key = lambda k: _paint_client(f"{k:<30}", "94", color)
    ok = lambda s: _paint_client(s, "92", color)
    warn = lambda s: _paint_client(s, "93", color)

    requester_user = file_config.user or os.environ.get("USER", "")
    client_default_route = file_config.route or "-"
    worker_route = req_route or file_config.worker_route or file_config.route or "-"
    request_route = req_route or "-"
    interactive_command = file_config.interactive_command or DEFAULT_INTERACTIVE_COMMAND
    interactive_source_bashrc = (
        file_config.interactive_source_bashrc
        if file_config.interactive_source_bashrc is not None
        else False
    )
    worker_max_jobs = file_config.worker_max_jobs if file_config.worker_max_jobs is not None else 4
    worker_poll_interval = file_config.worker_poll_interval if file_config.worker_poll_interval is not None else 0.05
    worker_debug = file_config.worker_debug if file_config.worker_debug is not None else False

    worker_count = len(active_workers)
    worker_count_text = ok(str(worker_count)) if worker_count > 0 else warn("0")
    bashrc_text = ok("true") if interactive_source_bashrc else warn("false")
    debug_text = ok("true") if worker_debug else warn("false")
    remote_env_text = _format_remote_env(file_config.remote_env)
    worker_lines = _format_active_workers(active_workers)

    lines: List[str] = []
    lines.append("")
    lines.append(line)
    lines.append(title)
    lines.append(line)
    lines.append(section("Session"))
    lines.append(f"  {key('session_id')}: {session_id}")
    lines.append(f"  {key('mode')}: interactive")
    lines.append(f"  {key('cwd')}: {cwd}")
    lines.append(f"  {key('remote.exec')}: {_format_exec_preview(remote_command)}")
    lines.append("")
    lines.append(section("Routing"))
    lines.append(f"  {key('cache')}: {config.cache_root}")
    lines.append(f"  {key('namespace')}: {config.namespace}")
    lines.append(f"  {key('route(request)')}: {request_route}")
    lines.append(f"  {key('route(client default)')}: {client_default_route}")
    lines.append(f"  {key('route(worker target)')}: {worker_route}")
    lines.append("")
    lines.append(section("Config (effective)"))
    lines.append(f"  {key('config.path')}: {str(file_config.path) if file_config.path is not None else '<unset>'}")
    lines.append(f"  {key('user')}: {requester_user or '-'}")
    lines.append(f"  {key('interactive.command')}: {interactive_command}")
    lines.append(f"  {key('interactive.source_bashrc')}: {bashrc_text}")
    lines.append(f"  {key('worker.max_jobs')}: {worker_max_jobs}")
    lines.append(f"  {key('worker.poll_interval')}: {worker_poll_interval}")
    lines.append(f"  {key('worker.debug')}: {debug_text}")
    lines.append(f"  {key('remote_env')}: {remote_env_text}")
    lines.append("")
    lines.append(section("Active Workers"))
    lines.append(f"  {key('count')}: {worker_count_text}")
    for idx, info in enumerate(worker_lines):
        if idx == 0:
            lines.append(f"  {key('workers')}: {info}")
        else:
            lines.append(f"  {key('')}: {info}")
    lines.append(line)
    return "\n".join(lines) + "\n"


def _print_interactive_panel(
    *,
    session_id: str,
    config: PigeonConfig,
    cwd: str,
    req_route: Optional[str],
    file_config: FileConfig,
    active_workers: Sequence[Dict[str, object]],
    remote_command: Sequence[str],
) -> None:
    panel = _format_interactive_panel(
        session_id=session_id,
        config=config,
        cwd=cwd,
        req_route=req_route,
        file_config=file_config,
        active_workers=active_workers,
        remote_command=remote_command,
    )
    print(panel, file=sys.stderr, flush=True)


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


def run_command(command: List[str], parsed_args: argparse.Namespace, command_mode: str = "argv") -> int:
    if command_mode not in {"argv", "shell_snippet", "interactive"}:
        raise RuntimeError(f"invalid command mode: {command_mode}")
    if command_mode in {"argv", "shell_snippet"} and not command:
        print("usage: pigeon <cmd...>", file=sys.stderr)
        return 2
    if command_mode == "argv":
        bad = _find_ambiguous_operator_token(command)
        if bad is not None:
            snippet = " ".join(str(x) for x in command)
            print(
                f"pigeon: ambiguous shell operator token {bad!r} in argv mode",
                file=sys.stderr,
            )
            print(
                f"pigeon: use shell mode instead: pigeon -c {shlex.quote(snippet)}",
                file=sys.stderr,
            )
            return 2

    file_config, created, _ = sync_env_to_file_config(None)
    if created:
        print(f"[pigeon] initialized config: {file_config.path}", file=sys.stderr)
    config = PigeonConfig.from_sources(file_config)
    config.ensure_dirs()
    req_route = _resolve_request_route(parsed_args, file_config)
    wait_timeout = _resolve_worker_wait_timeout(parsed_args)
    active_workers = _wait_for_worker(config, req_route, wait_timeout)
    if not active_workers:
        route_label = req_route or "-"
        print(
            (
                f"[pigeon] no active worker found within {wait_timeout:.1f}s "
                f"(namespace={config.namespace} route={route_label} cache={config.cache_root})"
            ),
            file=sys.stderr,
        )
        if req_route:
            print(f"[pigeon] start worker: pigeon worker --route {req_route}", file=sys.stderr)
        else:
            print("[pigeon] start worker: pigeon worker", file=sys.stderr)
        return 4

    cwd = str(Path.cwd().resolve())
    session_id = new_session_id()
    normalized_command = _normalize_exec_command(command, file_config, command_mode=command_mode)
    if command_mode == "interactive":
        _print_interactive_panel(
            session_id=session_id,
            config=config,
            cwd=cwd,
            req_route=req_route,
            file_config=file_config,
            active_workers=active_workers,
            remote_command=normalized_command,
        )
    sdir = session_dir(config, session_id)
    sdir.mkdir(parents=True, exist_ok=False)

    terminal_env, terminal_unset = _terminal_env_patch()
    extra_env = dict(terminal_env)
    if command_mode == "interactive":
        extra_env.update(_interactive_extra_env())

    request = _build_request(
        command=normalized_command,
        cwd=cwd,
        session_id=session_id,
        file_config=file_config,
        route=req_route,
        extra_env=extra_env,
        unset_env=terminal_unset,
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
    pending_deadline = now_ts() + max(wait_timeout, 0.0)
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

            if state == "pending":
                workers = discover_active_workers(config, req_route)
                if workers:
                    pending_deadline = now_ts() + max(wait_timeout, 0.0)
                elif now_ts() >= pending_deadline:
                    route_label = req_route or "-"
                    print(
                        (
                            f"\n[pigeon] session={session_id} is still pending and no active worker "
                            f"(namespace={config.namespace} route={route_label})"
                        ),
                        file=sys.stderr,
                    )
                    return 4

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
