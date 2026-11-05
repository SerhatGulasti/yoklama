"""Foreground daemon: filters file open events and records package use."""

import logging
import os
import resource
import signal
import sqlite3
import time
from collections import Counter
from collections.abc import Callable

from . import catalog as catalog_module
from .catalog import Catalog
from .store import Sighting, Store

PACMAN_LOCK = "/var/lib/pacman/db.lck"
PACMAN_WINDOW_TAIL = 10.0
COMM_LENGTH = 15
UNKNOWN_PROC = "?"

log = logging.getLogger(__name__)


def read_config_lines(path: str) -> list[str]:
    with open(path, encoding="utf-8") as f:
        lines = (line.strip() for line in f)
        return [line for line in lines if line and not line.startswith("#")]


def load_ignored_procs(path: str) -> frozenset[str]:
    return frozenset(name[:COMM_LENGTH] for name in read_config_lines(path))


def load_ignored_paths(path: str) -> tuple[bytes, ...]:
    return tuple(
        os.fsencode(line.rstrip("/") + "/") for line in read_config_lines(path)
    )


def read_comm(pid: int) -> str | None:
    """Name of a process, or None if it has already exited."""
    try:
        fd = os.open(f"/proc/{pid}/comm", os.O_RDONLY | os.O_CLOEXEC)
    except OSError:
        return None
    try:
        return os.read(fd, 64).rstrip(b"\n").decode("utf-8", "replace")
    except OSError:
        return None
    finally:
        os.close(fd)


def pacman_running() -> bool:
    return any(
        entry.isdigit() and read_comm(int(entry)) == "pacman"
        for entry in os.listdir("/proc")
    )


class PacmanWindow:
    """True from when pacman holds its lock until `tail` seconds after it stops.

    A lock with no pacman process is stale and opens nothing, so that a crashed
    transaction cannot blind the daemon.
    """

    RECHECK_RUNNING = 1.0
    RECHECK_STALE = 5.0

    def __init__(
        self,
        lock_path: str = PACMAN_LOCK,
        tail: float = PACMAN_WINDOW_TAIL,
        clock: Callable[[], float] = time.monotonic,
        is_running: Callable[[], bool] = pacman_running,
    ):
        self._lock_path = lock_path
        self._tail = tail
        self._clock = clock
        self._is_running = is_running
        self._until = float("-inf")
        self._next_check = float("-inf")
        self._running = False
        self._open = False
        self._stale_warned = False

    def active(self) -> bool:
        now = self._clock()
        if os.path.exists(self._lock_path):
            if now >= self._next_check:
                self._running = self._is_running()
                if self._running:
                    self._next_check = now + self.RECHECK_RUNNING
                    self._stale_warned = False
                else:
                    self._next_check = now + self.RECHECK_STALE
                    if not self._stale_warned:
                        log.warning("stale pacman lock: %s", self._lock_path)
                        self._stale_warned = True
            if self._running:
                self._until = now + self._tail
        else:
            self._running = False
            self._next_check = float("-inf")
            self._stale_warned = False

        is_open = now < self._until
        if is_open != self._open:
            log.info("pacman window %s", "opened" if is_open else "closed")
            self._open = is_open
        return is_open


class EventFilter:
    """Decides whether one event counts as package use, and for which package."""

    def __init__(
        self,
        catalog: Catalog,
        ignored_procs: frozenset[str],
        own_pid: int,
        comm_reader: Callable[[int], str | None] = read_comm,
    ):
        self._paths = catalog.paths
        self._ignored_procs = ignored_procs
        self._own_pid = own_pid
        self._comm_reader = comm_reader

    def classify(
        self, pid: int, path: bytes | None
    ) -> tuple[str, str | None, str | None]:
        """Return (outcome, package, process). Cheapest checks run first."""
        if pid == self._own_pid:
            return "own", None, None
        if path is None:
            return "unresolved", None, None
        if not path.startswith(catalog_module.WATCHED_PREFIXES):
            return "unwatched", None, None
        package = self._paths.get(path)
        if package is None:
            return "uncataloged", None, None
        proc = self._comm_reader(pid)
        if proc is None:
            # Gone before we could ask. It is not known to be a maintenance
            # process, and those mostly run inside the pacman window.
            proc = UNKNOWN_PROC
        elif proc in self._ignored_procs:
            return "ignored_proc", None, None
        return "counted", package, proc


class Daemon:
    def __init__(
        self,
        watcher,
        store: Store,
        config_dir: str,
        local_db: str = catalog_module.LOCAL_DB,
        window: PacmanWindow | None = None,
        flush_interval: float = 60.0,
        stats: bool = False,
        wall_clock: Callable[[], float] = time.time,
        mono_clock: Callable[[], float] = time.monotonic,
    ):
        self._watcher = watcher
        self._store = store
        self._config_dir = config_dir
        self._local_db = local_db
        self._window = window or PacmanWindow()
        self._flush_interval = flush_interval
        self._stats = stats
        self._wall_clock = wall_clock
        self._mono_clock = mono_clock
        self._filter: EventFilter | None = None
        self._pending: dict[str, Sighting] = {}
        self._counts: Counter[str] = Counter()
        self._next_flush = mono_clock() + flush_interval
        self.stop_requested = False
        self.reload_requested = False

    def load_catalog(self) -> None:
        """Build the catalog and the filter.

        An unreadable config raises and changes nothing. The old catalog is
        released before the new one is built, because holding both raises the
        memory the process keeps by about 50 MiB. Events wait in the kernel queue
        meanwhile, so nothing depends on the old one. The cost: if the package
        database itself cannot be read, nothing counts until the next reload.
        """
        started = self._mono_clock()
        ignored_paths = load_ignored_paths(
            os.path.join(self._config_dir, "ignore_paths.txt")
        )
        ignored_procs = load_ignored_procs(
            os.path.join(self._config_dir, "ignore_procs.txt")
        )
        self._filter = None
        try:
            catalog = catalog_module.load(self._local_db, ignored_paths)
        except OSError:
            empty = Catalog({}, {})
            self._filter = EventFilter(empty, ignored_procs, os.getpid())
            raise
        self._filter = EventFilter(catalog, ignored_procs, os.getpid())
        self._store.record_reindex(int(self._wall_clock()))
        log.info(
            "catalog loaded: %d packages, %d paths, %.2fs",
            len(catalog.packages),
            len(catalog.paths),
            self._mono_clock() - started,
        )

    def _reload(self) -> None:
        self.reload_requested = False
        try:
            self.load_catalog()
        except (OSError, ValueError, sqlite3.Error) as e:
            log.error("reload failed: %s", e)

    def handle_events(self, events, now: int) -> None:
        for pid, path in events:
            outcome, package, proc = self._filter.classify(pid, path)
            self._counts[outcome] += 1
            if package is None:
                continue
            seen = self._pending.get(package)
            if seen is None:
                self._pending[package] = Sighting(now, now, 1, proc)
            else:
                seen.last_seen = now
                seen.hits += 1
                seen.last_proc = proc

    def flush(self) -> None:
        """Write pending sightings. On failure they stay pending for the next try."""
        if self._pending:
            try:
                self._store.write_sightings(self._pending)
            except sqlite3.Error as e:
                log.error("flush failed, will retry: %s", e)
            else:
                self._pending = {}
        if self._stats:
            self._log_stats()

    def _log_stats(self) -> None:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        log.info(
            "stats cpu=%.2fs maxrss=%dKiB events=%s",
            usage.ru_utime + usage.ru_stime,
            usage.ru_maxrss,
            dict(sorted(self._counts.items())),
        )

    def step(self, timeout: float = 1.0) -> None:
        if self.reload_requested:
            self._reload()

        ready = self._watcher.wait(timeout)
        in_window = self._window.active()
        if ready:
            events, overflows = self._watcher.read(resolve=not in_window)
            if overflows:
                self._counts["overflow"] += overflows
                log.warning("fanotify queue overflowed %d time(s)", overflows)
            if in_window:
                self._counts["pacman_window"] += len(events)
            else:
                self.handle_events(events, int(self._wall_clock()))

        if self._mono_clock() >= self._next_flush:
            self._next_flush = self._mono_clock() + self._flush_interval
            self.flush()

    def run(self) -> None:
        self._store.record_start(int(self._wall_clock()))
        self.load_catalog()
        log.info("watching, pid %d", os.getpid())
        while not self.stop_requested:
            self.step()
        self.flush()
        log.info("stopped: %s", dict(sorted(self._counts.items())))


def install_signal_handlers(daemon: Daemon) -> None:
    def stop(signum, frame):
        daemon.stop_requested = True

    def reload(signum, frame):
        daemon.reload_requested = True

    signal.signal(signal.SIGTERM, stop)
    signal.signal(signal.SIGINT, stop)
    signal.signal(signal.SIGHUP, reload)
