"""Command line entry point."""

import argparse
import logging
import os
import sys

from . import report as report_module
from .daemon import Daemon, install_signal_handlers
from .store import Store

DEFAULT_DB = "/var/lib/yoklama/yoklama.db"
DEFAULT_CONFIG_DIR = os.path.dirname(os.path.dirname(os.path.realpath(__file__)))


def run(args: argparse.Namespace) -> int:
    # Imported here so that the report needs no ctypes.
    from .watcher import Watcher

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        stream=sys.stderr,
    )
    # Readable by the unprivileged report command.
    os.umask(0o022)
    try:
        watcher = Watcher()
    except OSError as e:
        print(f"yoklama: {e}", file=sys.stderr)
        if os.geteuid() != 0:
            print("yoklama: run needs root", file=sys.stderr)
        return 1

    store = Store(args.db)
    daemon = Daemon(
        watcher,
        store,
        args.config_dir,
        flush_interval=args.flush_interval,
        stats=args.stats,
    )
    install_signal_handlers(daemon)
    try:
        daemon.run()
    finally:
        store.close()
        watcher.close()
    return 0


def report(args: argparse.Namespace) -> int:
    try:
        print(
            report_module.generate(
                args.db, args.config_dir, args.days, args.all, args.json
            )
        )
    except report_module.ReportError as e:
        print(f"yoklama: {e}", file=sys.stderr)
        return 1
    except BrokenPipeError:
        # The reader closed the pipe, as `yoklama report | head` does.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
    return 0


def non_negative_int(text: str) -> int:
    value = int(text)
    if value < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return value


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="yoklama")
    commands = parser.add_subparsers(dest="command", required=True)

    run_parser = commands.add_parser("run", help="watch file opens in the foreground")
    run_parser.add_argument("--db", default=DEFAULT_DB, help="database path")
    run_parser.add_argument(
        "--config-dir",
        default=DEFAULT_CONFIG_DIR,
        help="directory holding ignore_procs.txt and ignore_paths.txt",
    )
    run_parser.add_argument(
        "--flush-interval",
        type=float,
        default=60.0,
        help="seconds between database writes",
    )
    run_parser.add_argument(
        "--stats", action="store_true", help="log resource use at every flush"
    )
    run_parser.set_defaults(func=run)

    report_parser = commands.add_parser(
        "report", help="list packages not seen for a while"
    )
    report_parser.add_argument(
        "--days",
        type=non_negative_int,
        default=report_module.DEFAULT_DAYS,
        help="idle days before a package is listed (default: %(default)s)",
    )
    report_parser.add_argument(
        "--all",
        action="store_true",
        help="cover every package, not only explicit ones nothing depends on",
    )
    report_parser.add_argument("--json", action="store_true", help="print JSON")
    report_parser.add_argument("--db", default=DEFAULT_DB, help="database path")
    report_parser.add_argument(
        "--config-dir",
        default=DEFAULT_CONFIG_DIR,
        help="directory holding ignore_paths.txt and protected.txt",
    )
    report_parser.set_defaults(func=report)

    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
