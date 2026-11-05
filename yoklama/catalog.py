"""Path to package map, built from pacman's local database.

Paths are bytes throughout: file names need not be valid UTF-8, and comparing
bytes keeps the result independent of the locale the daemon runs under.
"""

import logging
import os
from dataclasses import dataclass

LOCAL_DB = "/var/lib/pacman/local"
WATCHED_PREFIXES = (b"/usr/", b"/etc/", b"/opt/")
# A package left with only these says nothing about its real work. zram-generator
# is the case in point: it works at boot from an ignored path and keeps only docs.
NON_USAGE_PREFIXES = (
    b"/usr/share/licenses/",
    b"/usr/share/doc/",
    b"/usr/share/man/",
)

log = logging.getLogger(__name__)


@dataclass(frozen=True, slots=True)
class PackageInfo:
    name: str
    install_date: int
    explicit: bool
    size: int
    # Files that could show the package doing its real work. Zero means use
    # cannot be measured, so the report must not make the package look unused.
    # This only sorts packages into report sections: opening a man page still
    # counts as a sighting.
    usage_files: int


@dataclass(frozen=True, slots=True)
class Catalog:
    paths: dict[bytes, str]
    packages: dict[str, PackageInfo]


def _parse_desc(data: bytes) -> dict[bytes, list[bytes]]:
    sections: dict[bytes, list[bytes]] = {}
    current = None
    for line in data.split(b"\n"):
        if line.startswith(b"%") and line.endswith(b"%"):
            current = sections.setdefault(line, [])
        elif line and current is not None:
            current.append(line)
    return sections


def _first_int(sections: dict[bytes, list[bytes]], key: bytes) -> int:
    values = sections.get(key)
    return int(values[0]) if values else 0


def _package_files(data: bytes):
    """Yield the absolute path of each regular entry in a `files` database file."""
    in_files = False
    for line in data.split(b"\n"):
        if line.startswith(b"%") and line.endswith(b"%"):
            in_files = line == b"%FILES%"
        elif in_files and line and not line.endswith(b"/"):
            yield b"/" + line


def _read(path: str) -> bytes:
    with open(path, "rb") as f:
        return f.read()


def load(
    local_db: str = LOCAL_DB,
    ignored_paths: tuple[bytes, ...] = (),
    watched_prefixes: tuple[bytes, ...] = WATCHED_PREFIXES,
) -> Catalog:
    """Build the catalog.

    Paths outside the watched prefixes and under the ignored directories are
    left out, so a single lookup decides whether an event can count as use and
    the map stays smaller.
    """
    paths: dict[bytes, str] = {}
    packages: dict[str, PackageInfo] = {}

    with os.scandir(local_db) as entries:
        for entry in entries:
            if not entry.is_dir():
                continue
            try:
                sections = _parse_desc(_read(os.path.join(entry.path, "desc")))
                files = _read(os.path.join(entry.path, "files"))
                name = sections[b"%NAME%"][0].decode()
                install_date = _first_int(sections, b"%INSTALLDATE%")
                explicit = _first_int(sections, b"%REASON%") != 1
                size = _first_int(sections, b"%SIZE%")
            except (OSError, KeyError, IndexError, ValueError) as e:
                log.warning("skipping package entry %s: %s", entry.name, e)
                continue

            usage_files = 0
            for path in _package_files(files):
                if path.startswith(watched_prefixes) and not path.startswith(
                    ignored_paths
                ):
                    paths[path] = name
                    if not path.startswith(NON_USAGE_PREFIXES):
                        usage_files += 1
            packages[name] = PackageInfo(
                name, install_date, explicit, size, usage_files
            )

    return Catalog(paths, packages)
