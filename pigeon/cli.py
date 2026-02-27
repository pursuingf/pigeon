from __future__ import annotations

import argparse
import os
import sys
from typing import List, Sequence, Tuple

from .client import run_command
from .config import (
    config_target_path,
    config_to_toml,
    configurable_keys,
    default_config_path,
    ensure_file_config,
    set_config_value,
    unset_config_value,
    write_file_config,
)
from .worker import run_worker


def _default_path_help() -> str:
    return f"Config path (default: $PIGEON_CONFIG, else {default_config_path()})"


def _worker_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pigeon worker", description="Run pigeon worker loop")
    p.add_argument("--config", type=str, default=None, help=_default_path_help())
    p.add_argument("--max-jobs", type=int, default=None, help="Max concurrent session runners")
    p.add_argument("--poll-interval", type=float, default=None, help="Worker discovery poll seconds")
    p.add_argument("--route", type=str, default=None, help="Worker route key (only pick matching routed tasks)")
    p.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Print worker debug events and byte-level I/O previews",
    )
    return p


def _run_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pigeon", description="Run command on remote worker via shared cache")
    p.add_argument("--config", type=str, default=None, help=_default_path_help())
    p.add_argument("-v", "--verbose", action="store_true", help="Print session state transitions")
    p.add_argument("--route", type=str, default=None, help="Route key for selecting worker group")
    return p


def _config_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pigeon config", description="View and edit pigeon config")
    p.add_argument("--config", type=str, default=None, help=_default_path_help())
    sub = p.add_subparsers(dest="action", required=True)

    sub.add_parser("init", help="Create config file with defaults if missing")
    sub.add_parser("path", help="Print resolved config path")
    keys = sub.add_parser("keys", help="List configurable keys")
    keys.add_argument("--short", action="store_true", help="Only print key names")

    show = sub.add_parser("show", help="Show config file values")
    show.add_argument("--effective", action="store_true", help="Also show effective values after env overrides")

    setp = sub.add_parser("set", help="Set one config key")
    setp.add_argument("key", type=str, help="Key name (run `pigeon config keys`)")
    setp.add_argument("value", type=str, help="Value string")

    unsetp = sub.add_parser("unset", help="Unset one config key")
    unsetp.add_argument("key", type=str, help="Key name (run `pigeon config keys`)")
    return p


def _split_client_args(args: Sequence[str]) -> Tuple[List[str], List[str]]:
    known: List[str] = []
    i = 0
    while i < len(args):
        tok = args[i]
        if tok == "--":
            return known, list(args[i + 1 :])
        if tok in {"-v", "--verbose"}:
            known.append(tok)
            i += 1
            continue
        if tok == "--route":
            if i + 1 >= len(args):
                known.append(tok)
                return known, []
            known.extend([tok, args[i + 1]])
            i += 2
            continue
        if tok.startswith("--route="):
            known.append(tok)
            i += 1
            continue
        if tok == "--config":
            if i + 1 >= len(args):
                known.append(tok)
                return known, []
            known.extend([tok, args[i + 1]])
            i += 2
            continue
        if tok.startswith("--config="):
            known.append(tok)
            i += 1
            continue
        return known, list(args[i:])
    return known, []


def _fmt_value(v: object) -> str:
    if v is None:
        return "<unset>"
    return str(v)


def _print_effective(file_cfg) -> None:
    cache = os.environ.get("PIGEON_CACHE") or file_cfg.cache
    namespace = os.environ.get("PIGEON_NAMESPACE") or file_cfg.namespace or file_cfg.user or os.environ.get("USER") or "default"
    requester_user = os.environ.get("PIGEON_USER") or file_cfg.user or os.environ.get("USER", "")
    client_route = os.environ.get("PIGEON_ROUTE") or file_cfg.route
    worker_route = (
        os.environ.get("PIGEON_WORKER_ROUTE")
        or os.environ.get("PIGEON_ROUTE")
        or file_cfg.worker_route
        or file_cfg.route
    )
    worker_max_jobs = file_cfg.worker_max_jobs if file_cfg.worker_max_jobs is not None else 4
    worker_poll = file_cfg.worker_poll_interval if file_cfg.worker_poll_interval is not None else 0.2
    worker_debug = file_cfg.worker_debug if file_cfg.worker_debug is not None else False

    print("[effective]")
    print(f"cache={_fmt_value(cache)}")
    print(f"namespace={_fmt_value(namespace)}")
    print(f"requester_user={_fmt_value(requester_user)}")
    print(f"client_route={_fmt_value(client_route)}")
    print(f"worker_route={_fmt_value(worker_route)}")
    print(f"worker.max_jobs={_fmt_value(worker_max_jobs)}")
    print(f"worker.poll_interval={_fmt_value(worker_poll)}")
    print(f"worker.debug={_fmt_value(worker_debug)}")


def _run_config(parsed_args: argparse.Namespace) -> int:
    try:
        target = config_target_path(parsed_args.config)
        action = parsed_args.action

        if action == "path":
            print(target)
            return 0

        if action == "keys":
            if parsed_args.short:
                for key in configurable_keys():
                    print(key)
                return 0
            print("configurable keys:")
            for key in configurable_keys():
                print(f"  {key}")
            return 0

        if action == "init":
            _, created = ensure_file_config(parsed_args.config)
            print(f"path={target}")
            print(f"created={'yes' if created else 'no'}")
            return 0

        if action == "show":
            cfg, created = ensure_file_config(parsed_args.config)
            print(f"path={target}")
            print("exists=yes")
            print(f"created_now={'yes' if created else 'no'}")
            body = config_to_toml(cfg).rstrip()
            print("")
            print("[file]")
            if body:
                print(body)
            else:
                print("# empty")
            if parsed_args.effective:
                print("")
                _print_effective(cfg)
            return 0

        cfg, _ = ensure_file_config(parsed_args.config)

        if action == "set":
            updated = set_config_value(cfg, parsed_args.key, parsed_args.value)
            written = write_file_config(updated, parsed_args.config)
            print(f"updated {written}: {parsed_args.key}")
            return 0

        if action == "unset":
            updated = unset_config_value(cfg, parsed_args.key)
            written = write_file_config(updated, parsed_args.config)
            print(f"updated {written}: {parsed_args.key} unset")
            return 0

        print(f"unknown config action: {action}", file=sys.stderr)
        return 2
    except Exception as exc:
        print(f"pigeon config error: {exc}", file=sys.stderr)
        return 2


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("usage: pigeon [--config FILE] [--route ROUTE] [-v] <cmd...>")
        print("       pigeon worker [--config FILE] [--max-jobs N] [--poll-interval S] [--debug|--no-debug]")
        print("       pigeon config [--config FILE] {init|path|show|set|unset|keys} ...")
        print("")
        print("cache/namespace config sources (priority high -> low):")
        print("  --config > PIGEON_CONFIG > default config path > env PIGEON_* overrides")
        print("optional env:")
        print("  PIGEON_CONFIG=/path/to/pigeon.toml")
        print(f"  PIGEON_DEFAULT_CONFIG=/path/to/pigeon.toml (default: {default_config_path()})")
        print("  PIGEON_CACHE=/path/to/shared/cache")
        print("  PIGEON_NAMESPACE=<namespace> (default: $USER)")
        print("  PIGEON_USER=<requester user>")
        print("  PIGEON_ROUTE=<client request route>")
        print("  PIGEON_WORKER_ROUTE=<worker consume route>")
        return 0 if args and args[0].startswith("-") else 2

    if args[0] == "worker":
        parsed = _worker_parser().parse_args(args[1:])
        return run_worker(parsed)

    if args[0] == "config":
        parsed = _config_parser().parse_args(args[1:])
        return _run_config(parsed)

    run_parser = _run_parser()
    known_args, command = _split_client_args(args)
    known = run_parser.parse_args(known_args)
    return run_command(command=command, parsed_args=known)


if __name__ == "__main__":
    raise SystemExit(main())
