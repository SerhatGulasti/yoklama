**EN** | [TR](README_TR.md)

# Yoklama

A small daemon for Arch-based systems that records when each pacman package was
last used. It watches package files being opened and reports the packages that
have not been seen for a long time.

Yoklama never removes a package and makes no recommendations. It only produces a
list; what you do with it is up to you.

## Requirements

- A Linux system with pacman and systemd
- Python 3.10 or newer (standard library only)
- `/usr`, `/etc` and `/opt` on the root mount (see [Limitations](#limitations))

## Installation

```sh
sudo ./install.sh
sudo systemctl enable --now yoklama
```

`install.sh` copies the files to `/usr/local/lib/yoklama`, owned by root, and puts
the service and the pacman hook in place. It does not enable or start the service.
Because the service runs as root, it does not run code from a user-writable location.

To try the installation without root, use `DESTDIR=/some/dir ./install.sh`.

## Usage

```sh
yoklama report                 # packages not seen for 60 days
yoklama report --days 90       # change the threshold
yoklama report --all           # every package, dependencies included
yoklama report --json          # JSON output for scripts
```

The report needs no root, only reads, and does not need the service to be running
at that moment. By default it covers explicitly installed packages that no other
package depends on (`pacman -Qet`).

```text
Observing since 2026-11-05. Scope: explicit packages nothing depends on.
WARNING: Observed for only 0.2 days, under the 30 this report needs to mean much.
WARNING: Nothing can reach 60 idle days yet, so an empty list says nothing.

Not seen for 60+ days (0)
  (none)

Protected, do not touch (0)
  (none)

Not measurable by use (2)
  PACKAGE          LAST SEEN  LAST PROCESS  INSTALLED      SIZE
  man-pages        never      -             2026-10-28  5.7 MiB
  vlc-plugins-all  never      -             2026-10-28      0 B
```

A package appears in one section only:

| Section | Contents |
| --- | --- |
| Not seen for N+ days | Packages past the threshold, oldest first. This is the actual candidate list. |
| Protected, do not touch | Packages that match `protected.txt` and are past the threshold. |
| Not measurable by use | Whatever the threshold, packages whose only watchable files are licenses, docs or man pages, or that have none at all. Not seeing them does not mean they are unused. |

For a package never seen, the clock starts at its install date or the start of
observation, whichever is later. Install dates come from the `installed` lines in
`/var/log/pacman.log`, because pacman's `%INSTALLDATE%` field changes at every upgrade.

The report warns when observation has run for less than 30 days. If it has also run
for less than the threshold, an empty list means nothing, because no package can have
reached the threshold yet.

## How it works

The daemon puts a fanotify mark on the root mount and listens for file opens. An
event counts as use of a package only if it passes these checks:

1. It was not caused by Yoklama itself.
2. It falls outside the pacman window. The window is open while pacman holds its
   `db.lck` lock and for 10 seconds after it stops. A stale lock with no pacman
   process does not open the window; it only logs a warning.
3. The file is under `/usr`, `/etc` or `/opt`.
4. The file belongs to a package and is not in a directory from `ignore_paths.txt`.
5. The process that opened it is not in `ignore_procs.txt`.

Events that pass are collected in memory and written to SQLite every 60 seconds and
at shutdown (`/var/lib/yoklama/yoklama.db`). For each package it keeps the first and
last time seen, the number of opens and the last process name. The file-to-package map
is built from pacman's local database, and the pacman hook reloads the service after
each transaction to refresh it.

| Module | Purpose |
| --- | --- |
| `yoklama/watcher.py` | fanotify through ctypes |
| `yoklama/catalog.py` | File-to-package map |
| `yoklama/daemon.py` | Event filter, pacman window, main loop |
| `yoklama/store.py` | SQLite storage |
| `yoklama/report.py` | The `report` command |

Fanotify is used directly because FID marks fail with `EXDEV` on btrfs subvolumes,
which is why fatrace sees no events from them.

## Configuration

The files live in the repository, but the service and the report read the copies under
`/usr/local/lib/yoklama`. After editing them:

```sh
sudo ./install.sh
sudo systemctl reload yoklama
```

| File | Contents | Used by |
| --- | --- | --- |
| `ignore_procs.txt` | Processes whose file opens never count as use. Matched on the first 15 characters, because the kernel truncates process names there. | daemon |
| `ignore_paths.txt` | Directories that get scanned wholesale. Opening a file under one never counts as use. | daemon, report |
| `protected.txt` | Packages the report keeps separate. One glob per line. | report |

If the code changed, `reload` is not enough; run `sudo systemctl restart yoklama`.

`yoklama run` options: `--db`, `--config-dir`, `--flush-interval` (seconds), and
`--stats`, which logs resource use at every write. For logs, use
`journalctl -u yoklama`.

## Limitations

The report is a hint, not a verdict. Check for yourself before removing a package.

- **Not seen does not mean not used.** Files read during boot before the service
  starts, and files the kernel loads itself, can be missed. Packages such as `amd-ucode`,
  `sof-firmware` and `bluez-hid2hci` can land on the candidate list for this reason.
  Long-running processes that opened their files before the service started and never
  open them again are invisible too.
- **Seen does not mean used.** Any process opening a file counts. If an application
  that scans files is not in `ignore_procs.txt`, the packages it scans look used. To spot
  this, check the `LAST PROCESS` column in the output of `yoklama report --all --days 0`.
- **Only the root mount is watched.** If `/usr`, `/etc` or `/opt` is on a separate mount,
  opens there are invisible. For the same reason, do not add settings that give the
  service its own mount namespace, such as `ProtectSystem` or `PrivateTmp`
  (see `systemd/yoklama.service`).
- **Nothing is observed while the service is stopped.** Downtime is not subtracted from
  the observation period.
- **Some data can be lost.** If the process is killed, records since the last write
  are gone. If the fanotify queue overflows, those events are lost and a warning is logged.

## Development

```sh
python3 -B -m unittest discover -s tests
```

The tests need neither root nor a running service.

## Uninstalling

```sh
sudo systemctl disable --now yoklama
sudo rm -r /usr/local/lib/yoklama
sudo rm /usr/local/bin/yoklama /etc/systemd/system/yoklama.service /etc/pacman.d/hooks/yoklama.hook
sudo systemctl daemon-reload
```

Collected data stays under `/var/lib/yoklama`; delete it too if you don't want it.
