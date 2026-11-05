"""The report command: which packages have not been seen for a while.

Reads only. It removes nothing and recommends nothing; what to do with the list
is the reader's call. It does not need the daemon to be running.
"""

import datetime
import fnmatch
import json
import os
import pathlib
import re
import sqlite3
import subprocess
import time
from dataclasses import asdict, dataclass

from . import catalog as catalog_module
from .catalog import Catalog
from .daemon import load_ignored_paths, read_config_lines

DAY = 86400
MINIMUM_OBSERVATION_DAYS = 30
DEFAULT_DAYS = 60
PACMAN_LOG = "/var/log/pacman.log"

_INSTALLED_LINE = re.compile(r"^\[([^\]]+)\] \[ALPM\] installed (\S+) \(")


class ReportError(Exception):
    """A problem to show the user as one line, without a traceback."""


@dataclass(frozen=True, slots=True)
class Row:
    name: str
    last_seen: int | None
    last_proc: str | None
    install_date: int
    size: int


@dataclass(frozen=True, slots=True)
class Report:
    generated_at: int
    observing_since: int
    days: int
    all_packages: bool
    idle: list[Row]
    protected: list[Row]
    unmeasurable: list[Row]

    @property
    def observed_days(self) -> float:
        return (self.generated_at - self.observing_since) / DAY


def _idle_since(install_date: int, last_seen: int | None, observing_since: int) -> int:
    """When the idle clock started for a package.

    A package never seen started at whichever is later, its install or the start
    of observation: before that there was no chance of seeing it.
    """
    if last_seen is not None:
        return last_seen
    return max(install_date, observing_since)


def build_report(
    catalog: Catalog,
    sightings: dict[str, tuple[int, str]],
    installs: dict[str, int],
    scope: set[str] | None,
    protected_patterns: list[str],
    observing_since: int,
    now: int,
    days: int,
) -> Report:
    """Sort the packages in `scope` (None: all of them) into the report's sections.

    Protected packages come first, so a package is never in more than one section
    and never looks like a candidate. Unmeasurable ones are listed whatever the
    threshold, because no amount of waiting will produce a sighting for them.

    A package missing from `installs` counts as installed when observation began.
    """
    timed: list[tuple[int, Row]] = []
    protected: list[tuple[int, Row]] = []
    unmeasurable: list[Row] = []

    for name, info in catalog.packages.items():
        if scope is not None and name not in scope:
            continue
        last_seen, last_proc = sightings.get(name, (None, None))
        install_date = installs.get(name, observing_since)
        row = Row(name, last_seen, last_proc, install_date, info.size)
        since = _idle_since(install_date, last_seen, observing_since)
        is_idle = now - since >= days * DAY

        if any(fnmatch.fnmatchcase(name, p) for p in protected_patterns):
            if is_idle:
                protected.append((since, row))
        elif info.usage_files == 0:
            unmeasurable.append(row)
        elif is_idle:
            timed.append((since, row))

    def oldest_first(items: list[tuple[int, Row]]) -> list[Row]:
        return [row for _, row in sorted(items, key=lambda i: (i[0], i[1].name))]

    return Report(
        now,
        observing_since,
        days,
        scope is None,
        oldest_first(timed),
        oldest_first(protected),
        sorted(unmeasurable, key=lambda r: r.name),
    )


def read_install_dates(path: str = PACMAN_LOG) -> dict[str, int]:
    """Latest time each package was installed, from pacman's log.

    pacman's own %INSTALLDATE% changes at every upgrade, so a package that is
    never used but is upgraded often would never look idle. Only "installed"
    lines count here, not "upgraded" or "reinstalled".
    """
    installs: dict[str, int] = {}
    try:
        with open(path, encoding="utf-8", errors="replace") as log:
            for line in log:
                match = _INSTALLED_LINE.match(line)
                if match is None:
                    continue
                try:
                    when = datetime.datetime.fromisoformat(match[1])
                except ValueError:
                    continue
                installs[match[2]] = int(when.timestamp())
    except OSError as e:
        # Without it recently installed packages could look idle. Better no
        # report than a wrong list.
        raise ReportError(f"cannot read {path}: {e}") from e
    return installs


def read_database(path: str) -> tuple[dict[str, tuple[int, str]], int]:
    """Return sightings by package name and the time observation began."""
    if not os.path.exists(path):
        raise ReportError(f"no database at {path}; has yoklama.service run?")
    conn = sqlite3.connect(f"{pathlib.Path(path).resolve().as_uri()}?mode=ro", uri=True)
    try:
        sightings = {
            name: (last_seen, last_proc)
            for name, last_seen, last_proc in conn.execute(
                "SELECT name, last_seen, last_proc FROM packages"
            )
        }
        started = conn.execute(
            "SELECT value FROM meta WHERE key = 'daemon_started_at'"
        ).fetchone()
    except sqlite3.Error as e:
        raise ReportError(f"cannot read {path}: {e}") from e
    finally:
        conn.close()
    if started is None:
        raise ReportError(f"{path} has no observation start time")
    return sightings, int(started[0])


def explicit_leaf_packages() -> set[str]:
    """Explicitly installed packages that nothing else depends on."""
    try:
        result = subprocess.run(
            ["pacman", "-Qetq"], capture_output=True, text=True, check=False
        )
    except OSError as e:
        raise ReportError(f"cannot run pacman: {e}") from e
    # pacman -Qet exits 1 when the list is empty, which is not an error here.
    if result.returncode not in (0, 1) or result.stderr.strip():
        raise ReportError(f"pacman -Qetq failed: {result.stderr.strip()}")
    return set(result.stdout.split())


def warnings(report: Report) -> list[str]:
    if report.observed_days >= MINIMUM_OBSERVATION_DAYS:
        return []
    lines = [
        f"Observed for only {report.observed_days:.1f} days, "
        f"under the {MINIMUM_OBSERVATION_DAYS} this report needs to mean much."
    ]
    if report.observed_days < report.days:
        lines.append(
            f"Nothing can reach {report.days} idle days yet, "
            "so an empty list says nothing."
        )
    return lines


def _date(timestamp: int | None) -> str:
    if timestamp is None:
        return "never"
    return time.strftime("%Y-%m-%d", time.localtime(timestamp))


def _size(size: int) -> str:
    value = float(size)
    for unit in ("B", "KiB", "MiB"):
        if value < 1024:
            return f"{value:.0f} {unit}" if unit == "B" else f"{value:.1f} {unit}"
        value /= 1024
    return f"{value:.1f} GiB"


def _table(rows: list[Row]) -> list[str]:
    if not rows:
        return ["  (none)"]
    header = ("PACKAGE", "LAST SEEN", "LAST PROCESS", "INSTALLED", "SIZE")
    cells = [
        (r.name, _date(r.last_seen), r.last_proc or "-", _date(r.install_date), _size(r.size))
        for r in rows
    ]
    widths = [max(len(row[i]) for row in [header, *cells]) for i in range(5)]

    def line(row: tuple[str, ...]) -> str:
        left = "  ".join(row[i].ljust(widths[i]) for i in range(4))
        return f"  {left}  {row[4].rjust(widths[4])}"

    return [line(header), *(line(row) for row in cells)]


def format_text(report: Report) -> str:
    scope = "all packages" if report.all_packages else "explicit packages nothing depends on"
    lines = [
        f"Observing since {_date(report.observing_since)}. Scope: {scope}.",
    ]
    lines += [f"WARNING: {w}" for w in warnings(report)]
    sections = (
        (f"Not seen for {report.days}+ days", report.idle),
        ("Protected, do not touch", report.protected),
        ("Not measurable by use", report.unmeasurable),
    )
    for title, rows in sections:
        lines += ["", f"{title} ({len(rows)})", *_table(rows)]
    return "\n".join(lines)


def format_json(report: Report) -> str:
    return json.dumps(
        {
            "generated_at": report.generated_at,
            "observing_since": report.observing_since,
            "observed_days": round(report.observed_days, 2),
            "days": report.days,
            "all_packages": report.all_packages,
            "warnings": warnings(report),
            "idle": [asdict(r) for r in report.idle],
            "protected": [asdict(r) for r in report.protected],
            "unmeasurable": [asdict(r) for r in report.unmeasurable],
        },
        indent=2,
    )


def generate(
    db: str, config_dir: str, days: int, all_packages: bool, as_json: bool
) -> str:
    sightings, observing_since = read_database(db)
    installs = read_install_dates()
    try:
        ignored = load_ignored_paths(os.path.join(config_dir, "ignore_paths.txt"))
        protected = read_config_lines(os.path.join(config_dir, "protected.txt"))
        catalog = catalog_module.load(ignored_paths=ignored)
    except OSError as e:
        raise ReportError(str(e)) from e
    scope = None if all_packages else explicit_leaf_packages()
    report = build_report(
        catalog,
        sightings,
        installs,
        scope,
        protected,
        observing_since,
        int(time.time()),
        days,
    )
    return format_json(report) if as_json else format_text(report)
