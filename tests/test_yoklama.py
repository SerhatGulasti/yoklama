"""Tests that need neither root nor a real fanotify group."""

import datetime
import json
import logging
import os
import signal
import sqlite3
import struct
import sys
import tempfile
import unittest
import unittest.mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.realpath(__file__))))

from yoklama import catalog
from yoklama.daemon import (
    Daemon,
    EventFilter,
    PacmanWindow,
    install_signal_handlers,
    load_ignored_paths,
    load_ignored_procs,
    read_comm,
)
from yoklama.catalog import Catalog, PackageInfo
from yoklama.report import (
    DAY,
    ReportError,
    build_report,
    format_json,
    format_text,
    read_database,
    read_install_dates,
    warnings,
)
from yoklama.store import Sighting, Store
from yoklama.watcher import METADATA_VERSION, parse_events

logging.disable(logging.CRITICAL)


def add_package(root, name, files, reason=None, install_date=1000, size=2048):
    directory = os.path.join(root, f"{name}-1.0-1")
    os.makedirs(directory)
    desc = f"%NAME%\n{name}\n\n%INSTALLDATE%\n{install_date}\n\n%SIZE%\n{size}\n\n"
    if reason is not None:
        desc += f"%REASON%\n{reason}\n\n"
    with open(os.path.join(directory, "desc"), "w") as f:
        f.write(desc)
    with open(os.path.join(directory, "files"), "w") as f:
        f.write("%FILES%\n" + "".join(line + "\n" for line in files))
        f.write("\n%BACKUP%\netc/foo.conf\tabc123\n\n")


class CatalogTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name

    def test_maps_files_and_skips_directories(self):
        add_package(self.root, "foo", ["usr/", "usr/bin/", "usr/bin/foo"])
        result = catalog.load(self.root)
        self.assertEqual(result.paths, {b"/usr/bin/foo": "foo"})

    def test_backup_section_is_not_read_as_files(self):
        add_package(self.root, "foo", ["usr/bin/foo"])
        self.assertNotIn(b"/etc/foo.conf\tabc123", catalog.load(self.root).paths)

    def test_reason_one_means_dependency(self):
        add_package(self.root, "dep", ["usr/bin/dep"], reason=1)
        add_package(self.root, "manual", ["usr/bin/manual"])
        packages = catalog.load(self.root).packages
        self.assertFalse(packages["dep"].explicit)
        self.assertTrue(packages["manual"].explicit)

    def test_reads_package_info(self):
        add_package(self.root, "foo", ["usr/bin/foo"], install_date=1234, size=99)
        info = catalog.load(self.root).packages["foo"]
        self.assertEqual((info.install_date, info.size), (1234, 99))

    def test_leaves_out_unwatched_and_ignored_paths(self):
        add_package(
            self.root,
            "foo",
            ["usr/bin/foo", "var/lib/foo", "usr/share/icons/foo.png"],
        )
        result = catalog.load(self.root, (b"/usr/share/icons/",))
        self.assertEqual(result.paths, {b"/usr/bin/foo": "foo"})
        self.assertEqual(result.packages["foo"].usage_files, 1)

    def test_package_without_watched_files_is_kept_with_zero(self):
        add_package(self.root, "meta", [])
        self.assertEqual(catalog.load(self.root).packages["meta"].usage_files, 0)

    def test_license_files_are_mapped_but_do_not_make_a_package_measurable(self):
        add_package(
            self.root,
            "theme",
            ["usr/share/icons/a.png", "usr/share/licenses/theme/LICENSE"],
        )
        result = catalog.load(self.root, (b"/usr/share/icons/",))
        self.assertEqual(result.packages["theme"].usage_files, 0)
        self.assertIn(b"/usr/share/licenses/theme/LICENSE", result.paths)

    def test_docs_and_man_pages_do_not_make_a_package_measurable(self):
        add_package(
            self.root,
            "gen",
            [
                "usr/lib/systemd/gen",
                "usr/share/doc/gen/example.conf",
                "usr/share/man/man8/gen.8.gz",
            ],
        )
        result = catalog.load(self.root, (b"/usr/lib/systemd/",))
        self.assertEqual(result.packages["gen"].usage_files, 0)

    def test_opening_a_man_page_can_still_count_as_a_sighting(self):
        add_package(self.root, "pages", ["usr/share/man/man1/a.1.gz"])
        self.assertEqual(
            catalog.load(self.root).paths[b"/usr/share/man/man1/a.1.gz"], "pages"
        )

    def test_skips_broken_entry_and_files_outside_directories(self):
        add_package(self.root, "good", ["usr/bin/good"])
        os.makedirs(os.path.join(self.root, "broken-1.0-1"))
        with open(os.path.join(self.root, "ALPM_DB_VERSION"), "w") as f:
            f.write("9\n")
        self.assertEqual(list(catalog.load(self.root).packages), ["good"])

    def test_non_utf8_file_name_stays_as_bytes(self):
        add_package(self.root, "foo", [])
        files = os.path.join(self.root, "foo-1.0-1", "files")
        with open(files, "wb") as f:
            f.write(b"%FILES%\nusr/bin/caf\xe9\n")
        self.assertIn(b"/usr/bin/caf\xe9", catalog.load(self.root).paths)


class ConfigTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "list.txt")

    def write(self, text):
        with open(self.path, "w") as f:
            f.write(text)

    def test_process_names_are_cut_to_the_kernel_limit(self):
        self.write("# comment\n\nupdate-desktop-database\npacman\n")
        self.assertEqual(
            load_ignored_procs(self.path), {"update-desktop-", "pacman"}
        )

    def test_ignored_paths_match_whole_directories(self):
        self.write("/usr/share/icons\n/usr/lib/systemd/\n")
        self.assertEqual(
            load_ignored_paths(self.path),
            (b"/usr/share/icons/", b"/usr/lib/systemd/"),
        )


class WatcherParseTest(unittest.TestCase):
    @staticmethod
    def event(fd, pid):
        return struct.pack("=IBBHQii", 24, METADATA_VERSION, 0, 24, 0x20, fd, pid)

    def test_resolves_path_and_closes_descriptor(self):
        with tempfile.NamedTemporaryFile() as f:
            fd = os.open(f.name, os.O_RDONLY)
            events, overflows = parse_events(self.event(fd, 42))
            self.assertEqual(events, [(42, os.fsencode(os.path.realpath(f.name)))])
            self.assertEqual(overflows, 0)
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_closes_descriptor_without_resolving(self):
        with tempfile.NamedTemporaryFile() as f:
            fd = os.open(f.name, os.O_RDONLY)
            self.assertEqual(parse_events(self.event(fd, 42), resolve=False)[0], [(42, None)])
            with self.assertRaises(OSError):
                os.fstat(fd)

    def test_counts_overflow_events(self):
        self.assertEqual(parse_events(self.event(-1, 0)), ([], 1))

    def test_rejects_unknown_metadata_version(self):
        buffer = struct.pack("=IBBHQii", 24, 99, 0, 24, 0x20, -1, 0)
        with self.assertRaises(RuntimeError):
            parse_events(buffer)


class FilterTest(unittest.TestCase):
    def setUp(self):
        cat = catalog.Catalog({b"/usr/bin/foo": "foo"}, {})
        self.comms = {10: "firefox", 11: "pacman"}
        self.filter = EventFilter(
            cat, frozenset({"pacman"}), own_pid=1, comm_reader=self.comms.get
        )

    def test_counts_use(self):
        self.assertEqual(
            self.filter.classify(10, b"/usr/bin/foo"), ("counted", "foo", "firefox")
        )

    def test_process_that_exited_still_counts(self):
        self.assertEqual(
            self.filter.classify(99, b"/usr/bin/foo"), ("counted", "foo", "?")
        )

    def test_drops(self):
        cases = {
            "own": (1, b"/usr/bin/foo"),
            "unresolved": (10, None),
            "unwatched": (10, b"/home/serhat/foo"),
            "uncataloged": (10, b"/usr/bin/bar"),
            "ignored_proc": (11, b"/usr/bin/foo"),
        }
        for outcome, (pid, path) in cases.items():
            with self.subTest(outcome):
                self.assertEqual(self.filter.classify(pid, path)[0], outcome)

    def test_deleted_file_is_not_cataloged(self):
        self.assertEqual(
            self.filter.classify(10, b"/usr/bin/foo (deleted)")[0], "uncataloged"
        )


class ReadCommTest(unittest.TestCase):
    def test_reads_own_name(self):
        with open(f"/proc/{os.getpid()}/comm") as f:
            self.assertEqual(read_comm(os.getpid()), f.read().strip())

    def test_gone_process_is_none(self):
        self.assertIsNone(read_comm(2**31 - 1))


class PacmanWindowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.lock = os.path.join(self.tmp.name, "db.lck")
        self.now = 100.0
        self.running = True
        self.window = PacmanWindow(
            self.lock, 10.0, lambda: self.now, lambda: self.running
        )

    def take_lock(self):
        open(self.lock, "w").close()

    def drop_lock(self):
        os.unlink(self.lock)

    def test_closed_without_lock(self):
        self.assertFalse(self.window.active())

    def test_open_while_lock_held_by_pacman(self):
        self.take_lock()
        self.assertTrue(self.window.active())
        self.now += 60
        self.assertTrue(self.window.active())

    def test_stays_open_for_the_tail_then_closes(self):
        self.take_lock()
        self.window.active()
        self.drop_lock()
        self.now += 9.9
        self.assertTrue(self.window.active())
        self.now += 0.2
        self.assertFalse(self.window.active())

    def test_tail_counts_from_when_the_lock_was_last_seen(self):
        self.take_lock()
        self.window.active()
        # Sooner than the process recheck: the lock itself still extends the window.
        self.now += 0.5
        self.window.active()
        self.drop_lock()
        self.now += 9.9
        self.assertTrue(self.window.active())
        self.now += 0.2
        self.assertFalse(self.window.active())

    def test_stale_lock_opens_nothing(self):
        self.running = False
        self.take_lock()
        self.assertFalse(self.window.active())
        self.now += 60
        self.assertFalse(self.window.active())

    def test_stale_lock_checks_for_pacman_rarely(self):
        calls = []

        def running():
            calls.append(self.now)
            return False

        window = PacmanWindow(self.lock, 10.0, lambda: self.now, running)
        self.take_lock()
        for _ in range(4):
            window.active()
            self.now += 1
        self.assertEqual(len(calls), 1)

    def test_new_transaction_after_stale_lock_is_noticed_at_once(self):
        self.running = False
        self.take_lock()
        self.window.active()
        self.drop_lock()
        self.window.active()
        self.running = True
        self.take_lock()
        self.assertTrue(self.window.active())


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "sub", "yoklama.db")
        self.store = Store(self.path)
        self.addCleanup(self.store.close)

    def rows(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return conn.execute(
                "SELECT name, first_seen, last_seen, hits, last_proc FROM packages"
            ).fetchall()
        finally:
            conn.close()

    def meta(self):
        conn = sqlite3.connect(f"file:{self.path}?mode=ro", uri=True)
        try:
            return dict(conn.execute("SELECT key, value FROM meta"))
        finally:
            conn.close()

    def test_merges_repeated_sightings(self):
        self.store.write_sightings({"foo": Sighting(10, 20, 3, "a")})
        self.store.write_sightings({"foo": Sighting(30, 40, 2, "b")})
        self.assertEqual(self.rows(), [("foo", 10, 40, 5, "b")])

    def test_clock_going_back_keeps_latest_last_seen(self):
        self.store.write_sightings({"foo": Sighting(10, 50, 1, "a")})
        self.store.write_sightings({"foo": Sighting(5, 20, 1, "b")})
        self.assertEqual(self.rows()[0][2], 50)

    def test_first_start_is_kept_across_restarts(self):
        self.store.record_start(100)
        self.store.record_start(200)
        meta = self.meta()
        self.assertEqual(meta["daemon_started_at"], "100")
        self.assertEqual(meta["last_started_at"], "200")

    def test_data_survives_reopening(self):
        self.store.write_sightings({"foo": Sighting(1, 1, 1, "a")})
        self.store.close()
        self.store = Store(self.path)
        self.assertEqual(len(self.rows()), 1)


class ReportTest(unittest.TestCase):
    NOW = 1000 * DAY
    STARTED = NOW - 100 * DAY

    def catalog(self, **install_dates):
        self.installs = dict(install_dates)
        return Catalog(
            {},
            {
                # The catalog's own date plays no part: it changes at every upgrade.
                name: PackageInfo(name, self.NOW, True, 2048, 1)
                for name in install_dates
            },
        )

    def build(self, catalog, sightings=None, scope=None, protected=(), started=None, days=60):
        return build_report(
            catalog,
            sightings or {},
            getattr(self, "installs", {}),
            scope,
            list(protected),
            self.STARTED if started is None else started,
            self.NOW,
            days,
        )

    def names(self, rows):
        return [r.name for r in rows]

    def test_seen_package_idles_from_its_last_sighting(self):
        cat = self.catalog(old=0, recent=0)
        sightings = {"old": (self.NOW - 61 * DAY, "a"), "recent": (self.NOW - 59 * DAY, "b")}
        self.assertEqual(self.names(self.build(cat, sightings).idle), ["old"])

    def test_threshold_is_inclusive(self):
        cat = self.catalog(foo=0)
        sightings = {"foo": (self.NOW - 60 * DAY, "a")}
        self.assertEqual(self.names(self.build(cat, sightings).idle), ["foo"])

    def test_never_seen_counts_from_observation_when_install_is_older(self):
        cat = self.catalog(foo=0)
        self.assertEqual(self.names(self.build(cat).idle), ["foo"])
        self.assertEqual(self.build(cat, started=self.NOW - 59 * DAY).idle, [])

    def test_never_seen_counts_from_install_when_newer_than_observation(self):
        self.assertEqual(self.build(self.catalog(foo=self.NOW - 59 * DAY)).idle, [])

    def test_recently_upgraded_package_never_used_still_looks_idle(self):
        # pacman's date says today, the log says it was installed long ago.
        cat = Catalog({}, {"foo": PackageInfo("foo", self.NOW, True, 1, 1)})
        self.installs = {"foo": 0}
        self.assertEqual(self.names(self.build(cat).idle), ["foo"])

    def test_package_missing_from_the_log_counts_from_observation(self):
        cat = Catalog({}, {"foo": PackageInfo("foo", 0, True, 1, 1)})
        self.installs = {}
        report = self.build(cat, started=self.NOW - 61 * DAY)
        self.assertEqual(report.idle[0].install_date, self.NOW - 61 * DAY)
        self.assertEqual(self.build(cat, started=self.NOW - 59 * DAY).idle, [])

    def test_installed_column_comes_from_the_log(self):
        cat = Catalog({}, {"foo": PackageInfo("foo", self.NOW, True, 1, 1)})
        self.installs = {"foo": 5 * DAY}
        self.assertEqual(self.build(cat).idle[0].install_date, 5 * DAY)

    def test_oldest_comes_first(self):
        cat = self.catalog(a=0, b=0)
        sightings = {"a": (self.NOW - 70 * DAY, "x"), "b": (self.NOW - 90 * DAY, "x")}
        self.assertEqual(self.names(self.build(cat, sightings).idle), ["b", "a"])

    def test_scope_limits_every_section(self):
        cat = self.catalog(inside=0, outside=0)
        report = self.build(cat, scope={"inside"})
        self.assertEqual(self.names(report.idle), ["inside"])
        self.assertFalse(report.all_packages)

    def test_no_scope_covers_all_packages(self):
        report = self.build(self.catalog(a=0, b=0))
        self.assertEqual(self.names(report.idle), ["a", "b"])
        self.assertTrue(report.all_packages)

    def test_protected_package_is_listed_apart_not_dropped(self):
        report = self.build(self.catalog(linux=0, foo=0), protected=["linux*"])
        self.assertEqual(self.names(report.protected), ["linux"])
        self.assertEqual(self.names(report.idle), ["foo"])

    def test_protected_package_in_use_is_not_listed(self):
        sightings = {"linux": (self.NOW, "a")}
        report = self.build(self.catalog(linux=0), sightings, protected=["linux*"])
        self.assertEqual(report.protected, [])

    def test_protected_globs_match_whole_name_and_case(self):
        report = self.build(self.catalog(mylinux=0, LINUX=0), protected=["linux*"])
        self.assertEqual(report.protected, [])

    def test_unmeasurable_is_listed_whatever_the_threshold(self):
        cat = Catalog({}, {"meta": PackageInfo("meta", self.NOW, True, 0, 0)})
        report = self.build(cat)
        self.assertEqual(self.names(report.unmeasurable), ["meta"])
        self.assertEqual(report.idle, [])

    def test_protected_beats_unmeasurable(self):
        cat = Catalog({}, {"base": PackageInfo("base", 0, True, 0, 0)})
        report = self.build(cat, protected=["base"])
        self.assertEqual(self.names(report.protected), ["base"])
        self.assertEqual(report.unmeasurable, [])

    def test_warns_under_thirty_days_of_observation(self):
        report = self.build(self.catalog(), started=self.NOW - 29 * DAY)
        self.assertEqual(len(warnings(report)), 2)
        self.assertIn("WARNING: Observed for only 29.0 days", format_text(report))

    def test_short_observation_past_threshold_warns_once(self):
        report = self.build(self.catalog(), started=self.NOW - 20 * DAY, days=10)
        self.assertEqual(len(warnings(report)), 1)

    def test_no_warning_from_thirty_days(self):
        report = self.build(self.catalog(), started=self.NOW - 30 * DAY)
        self.assertEqual(warnings(report), [])
        self.assertNotIn("WARNING", format_text(report))

    def test_text_shows_columns_and_never(self):
        text = format_text(self.build(self.catalog(foo=self.NOW - 200 * DAY)))
        self.assertIn("LAST PROCESS", text)
        row = next(line for line in text.splitlines() if line.startswith("  foo"))
        self.assertIn("never", row)
        self.assertIn("2.0 KiB", row)

    def test_text_names_empty_sections(self):
        self.assertIn("(none)", format_text(self.build(self.catalog())))

    def test_report_never_suggests_removal(self):
        report = self.build(self.catalog(foo=0), protected=["bar"])
        text = format_text(report).lower()
        self.assertNotIn("remove", text)
        self.assertNotIn("pacman -r", text)

    def test_json_round_trips(self):
        data = json.loads(format_json(self.build(self.catalog(foo=0))))
        self.assertEqual(data["idle"][0]["name"], "foo")
        self.assertIsNone(data["idle"][0]["last_seen"])
        self.assertEqual(data["days"], 60)


class InstallDatesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "pacman.log")

    def read(self, text):
        with open(self.path, "w") as f:
            f.write(text)
        return read_install_dates(self.path)

    def test_takes_the_time_of_installation(self):
        dates = self.read("[2026-10-28T16:07:42+0300] [ALPM] installed foo (1.0-1)\n")
        expected = datetime.datetime.fromisoformat("2026-10-28T16:07:42+03:00")
        self.assertEqual(dates, {"foo": int(expected.timestamp())})

    def test_upgrades_and_reinstalls_do_not_move_the_date(self):
        dates = self.read(
            "[2026-01-01T00:00:00+0000] [ALPM] installed foo (1.0-1)\n"
            "[2026-02-01T00:00:00+0000] [ALPM] upgraded foo (1.0-1 -> 2.0-1)\n"
            "[2026-03-01T00:00:00+0000] [ALPM] reinstalled foo (2.0-1)\n"
        )
        first = datetime.datetime(2026, 1, 1, tzinfo=datetime.timezone.utc)
        self.assertEqual(dates["foo"], int(first.timestamp()))

    def test_installing_again_after_removal_takes_the_later_date(self):
        dates = self.read(
            "[2026-01-01T00:00:00+0000] [ALPM] installed foo (1.0-1)\n"
            "[2026-02-01T00:00:00+0000] [ALPM] removed foo (1.0-1)\n"
            "[2026-03-01T00:00:00+0000] [ALPM] installed foo (1.0-1)\n"
        )
        last = datetime.datetime(2026, 3, 1, tzinfo=datetime.timezone.utc)
        self.assertEqual(dates["foo"], int(last.timestamp()))

    def test_ignores_other_lines_and_bad_timestamps(self):
        dates = self.read(
            "[2026-01-01T00:00:00+0000] [PACMAN] Running 'pacman -S installed'\n"
            "[2026-01-01T00:00:00+0000] [ALPM-SCRIPTLET] installed foo (1.0-1)\n"
            "[not a date] [ALPM] installed foo (1.0-1)\n"
            "garbage\n"
        )
        self.assertEqual(dates, {})

    def test_reads_the_older_log_date_format(self):
        self.assertIn("foo", self.read("[2019-01-01 10:00] [ALPM] installed foo (1.0-1)\n"))

    def test_unreadable_log_refuses_to_report(self):
        with self.assertRaisesRegex(ReportError, "cannot read"):
            read_install_dates(self.path)


class ReadDatabaseTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.path = os.path.join(self.tmp.name, "a b.db")

    def test_reads_sightings_and_start_time(self):
        store = Store(self.path)
        store.record_start(100)
        store.write_sightings({"foo": Sighting(1, 2, 3, "proc")})
        store.close()
        self.assertEqual(read_database(self.path), ({"foo": (2, "proc")}, 100))

    def test_missing_database_is_a_clear_error(self):
        with self.assertRaisesRegex(ReportError, "no database"):
            read_database(self.path)

    def test_does_not_create_a_database(self):
        with self.assertRaises(ReportError):
            read_database(self.path)
        self.assertFalse(os.path.exists(self.path))

    def test_database_without_start_time_is_a_clear_error(self):
        Store(self.path).close()
        with self.assertRaisesRegex(ReportError, "observation start"):
            read_database(self.path)

    def test_opening_read_only_leaves_the_file_alone(self):
        store = Store(self.path)
        store.record_start(100)
        store.close()
        before = os.stat(self.path).st_mtime_ns
        read_database(self.path)
        self.assertEqual(os.stat(self.path).st_mtime_ns, before)


class FakeWatcher:
    def __init__(self):
        self.batches = []
        self.resolve_args = []

    def wait(self, timeout):
        return bool(self.batches)

    def read(self, resolve=True):
        self.resolve_args.append(resolve)
        return self.batches.pop(0), 0


class FakeStore:
    def __init__(self):
        self.written = []
        self.fail = False
        self.reindexed = 0

    def write_sightings(self, sightings):
        if self.fail:
            raise sqlite3.OperationalError("locked")
        self.written.append({k: (v.hits, v.last_proc) for k, v in sightings.items()})

    def record_reindex(self, now):
        self.reindexed += 1

    def record_start(self, now):
        pass


class DaemonTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.config = os.path.join(self.tmp.name, "config")
        self.local = os.path.join(self.tmp.name, "local")
        os.makedirs(self.config)
        os.makedirs(self.local)
        self.write_config("ignore_procs.txt", "pacman\n")
        self.write_config("ignore_paths.txt", "/usr/share/icons\n")
        add_package(self.local, "foo", ["usr/bin/foo"])

        self.watcher = FakeWatcher()
        self.store = FakeStore()
        self.mono = 0.0
        self.in_window = False
        window = unittest.mock.Mock()
        window.active.side_effect = lambda: self.in_window
        self.daemon = Daemon(
            self.watcher,
            self.store,
            self.config,
            local_db=self.local,
            window=window,
            flush_interval=60,
            wall_clock=lambda: 1000.0,
            mono_clock=lambda: self.mono,
        )
        self.daemon.load_catalog()
        # This test process stands in for an ordinary program.
        self.pid = os.getppid()

    def write_config(self, name, text):
        with open(os.path.join(self.config, name), "w") as f:
            f.write(text)

    def test_buffers_in_memory_until_the_interval(self):
        self.watcher.batches.append([(self.pid, b"/usr/bin/foo")] * 2)
        self.daemon.step(0)
        self.assertEqual(self.store.written, [])
        self.mono = 60
        self.daemon.step(0)
        self.assertEqual(self.store.written[0]["foo"][0], 2)

    def test_quiet_flush_writes_nothing(self):
        self.mono = 60
        self.daemon.step(0)
        self.assertEqual(self.store.written, [])

    def test_window_drops_events_without_resolving_them(self):
        self.in_window = True
        self.watcher.batches.append([(self.pid, None)] * 5)
        self.daemon.step(0)
        self.assertEqual(self.watcher.resolve_args, [False])
        self.daemon.flush()
        self.assertEqual(self.store.written, [])
        self.assertEqual(self.daemon._counts["pacman_window"], 5)

    def test_failed_flush_keeps_sightings_for_retry(self):
        self.watcher.batches.append([(self.pid, b"/usr/bin/foo")])
        self.daemon.step(0)
        self.store.fail = True
        self.daemon.flush()
        self.store.fail = False
        self.daemon.flush()
        self.assertEqual(self.store.written[0]["foo"][0], 1)
        self.daemon.flush()
        self.assertEqual(len(self.store.written), 1)

    def test_ignored_path_never_counts(self):
        add_package(self.local, "icons", ["usr/share/icons/a.png"])
        self.daemon.load_catalog()
        self.watcher.batches.append([(self.pid, b"/usr/share/icons/a.png")])
        self.daemon.step(0)
        self.daemon.flush()
        self.assertEqual(self.store.written, [])

    def test_sighup_reload_finds_new_package(self):
        self.watcher.batches.append([(self.pid, b"/usr/bin/bar")])
        self.daemon.step(0)
        add_package(self.local, "bar", ["usr/bin/bar"])
        self.daemon.reload_requested = True
        self.watcher.batches.append([(self.pid, b"/usr/bin/bar")])
        self.daemon.step(0)
        self.daemon.flush()
        self.assertEqual(list(self.store.written[0]), ["bar"])
        self.assertEqual(self.store.reindexed, 2)
        self.assertFalse(self.daemon.reload_requested)

    def test_failed_reload_keeps_working(self):
        os.unlink(os.path.join(self.config, "ignore_procs.txt"))
        self.daemon.reload_requested = True
        self.watcher.batches.append([(self.pid, b"/usr/bin/foo")])
        self.daemon.step(0)
        self.daemon.flush()
        self.assertEqual(self.store.written[0]["foo"][0], 1)

    def test_unreadable_package_database_counts_nothing_until_next_reload(self):
        moved = self.local + ".moved"
        os.rename(self.local, moved)
        self.daemon.reload_requested = True
        self.watcher.batches.append([(self.pid, b"/usr/bin/foo")])
        self.daemon.step(0)
        self.daemon.flush()
        self.assertEqual(self.store.written, [])

        os.rename(moved, self.local)
        self.daemon.reload_requested = True
        self.watcher.batches.append([(self.pid, b"/usr/bin/foo")])
        self.daemon.step(0)
        self.daemon.flush()
        self.assertEqual(self.store.written[0]["foo"][0], 1)

    def test_signals_set_the_flags(self):
        previous = {
            s: signal.getsignal(s) for s in (signal.SIGTERM, signal.SIGINT, signal.SIGHUP)
        }
        self.addCleanup(lambda: [signal.signal(s, h) for s, h in previous.items()])
        install_signal_handlers(self.daemon)
        os.kill(os.getpid(), signal.SIGHUP)
        self.assertTrue(self.daemon.reload_requested)
        os.kill(os.getpid(), signal.SIGTERM)
        self.assertTrue(self.daemon.stop_requested)

    def test_run_flushes_pending_events_on_stop(self):
        self.watcher.batches.append([(self.pid, b"/usr/bin/foo")])
        original_step = self.daemon.step

        def step_then_stop():
            original_step(0)
            self.daemon.stop_requested = True

        self.daemon.step = step_then_stop
        self.daemon.run()
        self.assertEqual(self.store.written[0]["foo"][0], 1)


if __name__ == "__main__":
    unittest.main()
