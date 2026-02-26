from __future__ import annotations

import argparse
import sys
from typing import List, Sequence, Tuple

from .client import run_command
from .config import DEFAULT_CONFIG_PATH
from .worker import run_worker


def _worker_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pigeon worker", description="Run pigeon worker loop")
    p.add_argument(
        "--config",
        type=str,
        default=None,
        help=f"Config path (default: $PIGEON_CONFIG, else {DEFAULT_CONFIG_PATH})",
    )
    p.add_argument("--max-jobs", type=int, default=None, help="Max concurrent session runners")
    p.add_argument("--poll-interval", type=float, default=None, help="Worker discovery poll seconds")
    p.add_argument(
        "--debug",
        action=argparse.BooleanOptionalAction,
        default=None,
        help="Print worker debug events and byte-level I/O previews",
    )
    return p


def _run_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pigeon", description="Run command on remote worker via shared cache")
    p.add_argument(
        "--config",
        type=str,
        default=None,
        help=f"Config path (default: $PIGEON_CONFIG, else {DEFAULT_CONFIG_PATH})",
    )
    p.add_argument("-v", "--verbose", action="store_true", help="Print session state transitions")
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


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("usage: pigeon <cmd...>")
        print("       pigeon worker [--config FILE] [--max-jobs N] [--poll-interval S] [--debug|--no-debug]")
        print("")
        print("cache/namespace config sources (priority high -> low):")
        print("  --config > PIGEON_CONFIG > default config file > env PIGEON_* overrides")
        print("optional env:")
        print("  PIGEON_CONFIG=/path/to/pigeon.toml")
        print(f"  default config path: {DEFAULT_CONFIG_PATH}")
        print("  PIGEON_CACHE=/path/to/shared/cache")
        print("  PIGEON_NAMESPACE=<namespace> (default: $USER)")
        print("  PIGEON_USER=<requester user>")
        return 0 if args and args[0].startswith("-") else 2

    if args[0] == "worker":
        parsed = _worker_parser().parse_args(args[1:])
        return run_worker(parsed)

    run_parser = _run_parser()
    known_args, command = _split_client_args(args)
    known = run_parser.parse_args(known_args)
    return run_command(command=command, parsed_args=known)


if __name__ == "__main__":
    raise SystemExit(main())
