from __future__ import annotations

import argparse
import sys
from typing import Sequence

from .client import run_command
from .worker import run_worker


def _worker_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pigeon worker", description="Run pigeon worker loop")
    p.add_argument("--max-jobs", type=int, default=4, help="Max concurrent session runners")
    p.add_argument("--poll-interval", type=float, default=0.2, help="Worker discovery poll seconds")
    return p


def _run_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="pigeon", description="Run command on remote worker via shared cache")
    p.add_argument("-v", "--verbose", action="store_true", help="Print session state transitions")
    return p


def main(argv: Sequence[str] | None = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if not args or args[0] in {"-h", "--help"}:
        print("usage: pigeon <cmd...>")
        print("       pigeon worker [--max-jobs N] [--poll-interval S]")
        print("")
        print("required env:")
        print("  PIGEON_CACHE=/path/to/shared/cache")
        print("optional env:")
        print("  PIGEON_NAMESPACE=<namespace> (default: $USER)")
        return 0 if args and args[0].startswith("-") else 2

    if args[0] == "worker":
        parsed = _worker_parser().parse_args(args[1:])
        return run_worker(parsed)

    run_parser = _run_parser()
    known, command = run_parser.parse_known_args(args)
    return run_command(command=command, parsed_args=known)


if __name__ == "__main__":
    raise SystemExit(main())
