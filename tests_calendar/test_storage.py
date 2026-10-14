from __future__ import annotations

import os
import tempfile
import unittest
import unittest.mock
import urllib.parse
from pathlib import Path

from pony.calendar.domain import ResourceRef
from pony.calendar.storage import (
    MirrorError,
    ResourceNotFoundError,
    VdirMirrorRepository,
    safe_component,
)
from tests_calendar import corpus

ACCOUNT = "personal"
CALENDAR = "work"
OTHER_CALENDAR = "archive"


def _ref(uid: str, calendar: str = CALENDAR) -> ResourceRef:
    return ResourceRef(account_name=ACCOUNT, calendar_name=calendar, uid=uid)


class MirrorConformanceTest(unittest.TestCase):
    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(self.root)

    def test_list_returns_empty_when_account_or_calendar_missing(self) -> None:
        self.assertEqual(self.mirror.list_calendars(ACCOUNT), ())
        self.assertEqual(self.mirror.list_resources(ACCOUNT, CALENDAR), ())

    def test_write_then_read_round_trips_bytes(self) -> None:
        data = corpus.simple_event()
        ref = _ref("simple-event-1@example.com")
        self.mirror.write(ref, data)
        self.assertEqual(self.mirror.read(ref), data)

    def test_write_is_crash_safe_atomic_replace(self) -> None:
        ref = _ref("atomic@example.com")
        self.mirror.write(ref, b"first")
        self.mirror.write(ref, b"second")
        self.assertEqual(self.mirror.read(ref), b"second")
        leftovers = [
            p
            for p in self.mirror._path_for(ref).parent.iterdir()
            if p.name.startswith(".tmp-")
        ]
        self.assertEqual(leftovers, [])

    def test_write_creates_nested_dirs(self) -> None:
        ref = _ref("nested@example.com", calendar="deep/nested/calendar")
        self.mirror.write(ref, b"payload")
        self.assertTrue(self.mirror.exists(ref))

    def test_read_missing_raises(self) -> None:
        with self.assertRaises(ResourceNotFoundError):
            self.mirror.read(_ref("ghost@example.com"))

    def test_delete_missing_raises(self) -> None:
        with self.assertRaises(ResourceNotFoundError):
            self.mirror.delete(_ref("ghost@example.com"))

    def test_list_resources_after_writes(self) -> None:
        refs = [
            _ref("a@example.com"),
            _ref("b@example.com"),
            _ref("c+special@example.com"),
        ]
        for r in refs:
            self.mirror.write(r, b"payload")
        listed = self.mirror.list_resources(ACCOUNT, CALENDAR)
        self.assertEqual({r.uid for r in listed}, {r.uid for r in refs})

    def test_list_calendars_returns_subdirectories(self) -> None:
        self.mirror.write(_ref("a@example.com", "one"), b"x")
        self.mirror.write(_ref("b@example.com", "two"), b"y")
        self.assertEqual(self.mirror.list_calendars(ACCOUNT), ("one", "two"))

    def test_filename_preserves_non_ascii_uids(self) -> None:
        ref = _ref("café@example.com")
        self.mirror.write(ref, b"payload")
        listed = self.mirror.list_resources(ACCOUNT, CALENDAR)
        self.assertEqual(len(listed), 1)
        self.assertEqual(listed[0].uid, "café@example.com")

    def test_move_between_calendars(self) -> None:
        src = _ref("move@example.com", CALENDAR)
        dst = _ref("move@example.com", OTHER_CALENDAR)
        self.mirror.write(src, b"payload")
        self.mirror.move(src, dst)
        self.assertFalse(self.mirror.exists(src))
        self.assertEqual(self.mirror.read(dst), b"payload")

    def test_move_missing_source_raises(self) -> None:
        src = _ref("missing@example.com")
        dst = _ref("missing@example.com", OTHER_CALENDAR)
        with self.assertRaises(ResourceNotFoundError):
            self.mirror.move(src, dst)

    def test_round_trip_every_corpus_fixture(self) -> None:
        for name, data in corpus.ALL_SINGLE_FIXTURES:
            with self.subTest(fixture=name):
                ref = _ref(f"{name}@example.com")
                self.mirror.write(ref, data)
                self.assertEqual(self.mirror.read(ref), data)


class AccountMirrorPathTest(unittest.TestCase):
    """An account's configured `mirror_path` has to be where its files go.

    The setting was parsed, stored and written back to `config.toml` while
    the mirror ignored it entirely and always used `<root>/<account>`.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.root = self.tmp / "root"
        self.elsewhere = self.tmp / "elsewhere" / "calendars"
        self.mirror = VdirMirrorRepository(
            self.root, account_roots={ACCOUNT: self.elsewhere}
        )

    def test_writes_land_under_the_configured_path(self) -> None:
        ref = ResourceRef(ACCOUNT, CALENDAR, "a@example.com")
        self.mirror.write(ref, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
        written = list(self.elsewhere.rglob("*.ics"))
        self.assertEqual(len(written), 1, f"expected one file, found {written}")
        self.assertEqual(written[0].parent.name, CALENDAR)
        self.assertEqual(list(self.root.rglob("*.ics")), [])

    def test_reads_listings_and_deletes_follow_it(self) -> None:
        ref = ResourceRef(ACCOUNT, CALENDAR, "a@example.com")
        self.mirror.write(ref, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
        self.assertEqual(self.mirror.list_calendars(ACCOUNT), (CALENDAR,))
        self.assertEqual(
            [r.uid for r in self.mirror.list_resources(ACCOUNT, CALENDAR)],
            ["a@example.com"],
        )
        self.assertTrue(self.mirror.exists(ref))
        self.mirror.delete(ref)
        self.assertFalse(self.mirror.exists(ref))

    def test_an_account_without_one_keeps_the_default_layout(self) -> None:
        """The default is `<root>/<account>`, so nothing moves on upgrade."""
        other = "unconfigured"
        ref = ResourceRef(other, CALENDAR, "b@example.com")
        self.mirror.write(ref, b"BEGIN:VCALENDAR\r\nEND:VCALENDAR\r\n")
        self.assertTrue((self.root / other / CALENDAR).is_dir())
        self.assertEqual(list(self.elsewhere.rglob("b*.ics")), [])


class MirrorCrashSafetyTest(unittest.TestCase):
    """The mirror must survive a crash (KeyboardInterrupt, kill -9) at
    any point during a write: the previous file must be intact, and
    no half-written `.ics` or stale `.tmp-*` file may be left behind
    that would later be mistaken for real data.
    """

    def setUp(self) -> None:
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.mirror = VdirMirrorRepository(self.root)

    def _calendar_dir(self, calendar: str = CALENDAR) -> Path:
        return self.root / ACCOUNT / calendar

    def _leftovers(self, calendar: str = CALENDAR) -> list[str]:
        return [
            p.name
            for p in self._calendar_dir(calendar).iterdir()
            if p.name.startswith(".tmp-")
        ]

    def test_keyboard_interrupt_mid_write_preserves_prior_file(self) -> None:
        # Set up a known-good file, then arrange for the next write
        # to be interrupted mid-stream. The original file must remain
        # readable and identical, with no temp file behind.
        ref = _ref("interrupt@example.com")
        self.mirror.write(ref, b"original")

        original_replace = os.replace

        def boom(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        with (
            unittest.mock.patch("pony.calendar.storage.os.replace", side_effect=boom),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.mirror.write(ref, b"new bytes that would have replaced original")

        self.assertEqual(self.mirror.read(ref), b"original")
        self.assertEqual(self._leftovers(), [])
        # And the regular original_replace symbol still works on resume.
        self.assertIs(os.replace, original_replace)

    def test_first_write_failure_leaves_no_resource(self) -> None:
        # If the very first write to a resource is interrupted, the
        # resource must not exist (a partial file would be readable
        # later and misinterpreted as legitimate content).
        ref = _ref("never-saved@example.com")

        def boom(*_args: object, **_kwargs: object) -> None:
            raise KeyboardInterrupt

        with (
            unittest.mock.patch("pony.calendar.storage.os.replace", side_effect=boom),
            self.assertRaises(KeyboardInterrupt),
        ):
            self.mirror.write(ref, b"some content")

        self.assertFalse(self.mirror.exists(ref))
        self.assertEqual(self._leftovers(), [])

    def test_write_failure_before_replace_cleans_up_tmp(self) -> None:
        # If the write fails before os.replace (e.g. disk full mid-fsync),
        # the temp file must still be cleaned up so the directory
        # doesn't accumulate orphaned `.tmp-*` files across many
        # failed runs. fsync is inside the same try/finally as
        # os.replace, so patching it exercises the cleanup path.
        ref = _ref("disk-full@example.com")
        self.mirror.write(ref, b"original")

        with (
            unittest.mock.patch(
                "pony.calendar.storage.os.fsync", side_effect=OSError("disk full")
            ),
            self.assertRaises(OSError),
        ):
            self.mirror.write(ref, b"new content")

        # Original file unchanged.
        self.assertEqual(self.mirror.read(ref), b"original")
        # No temp leftovers.
        self.assertEqual(self._leftovers(), [])

    def test_list_resources_ignores_stale_tmp_files(self) -> None:
        # Even if a previous run died so abruptly that the
        # exception cleanup didn't get a chance to run (kill -9, OOM),
        # `list_resources` must not surface the orphaned `.tmp-*`
        # file as a real resource. The next sync's `_apply_server_deletions`
        # walks `list_resources`; a stale tmp would be treated as a
        # missing-from-server resource and trip the mass-deletion guard.
        good_ref = _ref("real@example.com")
        self.mirror.write(good_ref, b"real")
        # Drop a fake leftover tmp by hand — what `kill -9` mid-write
        # would leave behind.
        cal_dir = self._calendar_dir()
        leftover = cal_dir / ".tmp-deadbeef.ics"
        leftover.write_bytes(b"half written garbage")

        listed = self.mirror.list_resources(ACCOUNT, CALENDAR)
        self.assertEqual({r.uid for r in listed}, {"real@example.com"})


class UntrustedNameTest(unittest.TestCase):
    """Account and calendar names come from the server, so they are data.

    A CalDAV ``<D:displayname>`` is whatever the server sends.  Joined
    raw it was a directory traversal; a drive-qualified name discarded
    the mirror root on Windows, and a UNC name made the mkdir
    authenticate outwards.
    """

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name) / "mirror"
        self.mirror = VdirMirrorRepository(self.root)

    def _write(self, calendar_name: str) -> Path:
        ref = ResourceRef(
            account_name="acct",
            calendar_name=calendar_name,
            uid="event-1",
        )
        self.mirror.write(ref, corpus.simple_event())
        return self.root

    def test_a_traversing_name_stays_inside_the_mirror(self) -> None:
        self._write("../../../../etc/cron.d")
        written = [p for p in self.root.rglob("*.ics")]
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].is_relative_to(self.root.resolve()))

    def test_a_drive_qualified_name_stays_inside_the_mirror(self) -> None:
        self._write("C:\\Windows\\Temp")
        written = [p for p in self.root.rglob("*.ics")]
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].is_relative_to(self.root.resolve()))

    def test_a_unc_name_stays_inside_the_mirror(self) -> None:
        self._write("//attacker/share")
        written = [p for p in self.root.rglob("*.ics")]
        self.assertEqual(len(written), 1)
        self.assertTrue(written[0].is_relative_to(self.root.resolve()))

    def test_two_names_that_differ_never_share_a_directory(self) -> None:
        """The encoding is injective, so no two calendars can merge."""
        for name in ("Archive/2024", "Archive.2024", "Archive 2024"):
            self.mirror.write(
                ResourceRef(
                    account_name="acct",
                    calendar_name=name,
                    uid=f"uid-{name}",
                ),
                corpus.simple_event(),
            )
        account_dir = self.root / "acct"
        self.assertEqual(len(list(account_dir.iterdir())), 3)

    def test_a_name_survives_the_round_trip(self) -> None:
        for name in ("personal", "Work Calendar", "Archive/2024", "..", "NUL"):
            with self.subTest(name=name):
                self.mirror.write(
                    ResourceRef(
                        account_name="acct",
                        calendar_name=name,
                        uid="rt",
                    ),
                    corpus.simple_event(),
                )
                self.assertIn(name, self.mirror.list_calendars("acct"))
                refs = self.mirror.list_resources("acct", name)
                self.assertEqual([r.uid for r in refs], ["rt"])

    def test_an_empty_name_is_refused(self) -> None:
        with self.assertRaises(MirrorError):
            safe_component("")


class AtomicWriteTest(unittest.TestCase):
    """One dance, in `pony.atomic`, for every file either half replaces.

    It was written out five times inside this package and not at all in
    the mail half, which used `write_text` for its profile stamp and its
    `config.toml`.  Two things in it are easy to get subtly wrong when
    copied, and both are checked here: a failure must leave neither a
    truncated target nor a stray temporary, and an owner-only file must
    never exist at the ambient umask -- not even between its creation
    and a `chmod` after the rename, which is how the OAuth token store
    used to do it.
    """

    def setUp(self) -> None:
        self.tmp = Path(self.enterContext(tempfile.TemporaryDirectory()))

    def test_a_failure_leaves_the_old_file_and_no_leftover(self) -> None:
        from pony.atomic import write_atomically

        target = self.tmp / "thing.toml"
        write_atomically(target, b"first", suffix=".toml")
        with (
            unittest.mock.patch(
                "pony.atomic.os.replace", side_effect=KeyboardInterrupt
            ),
            self.assertRaises(KeyboardInterrupt),
        ):
            write_atomically(target, b"second", suffix=".toml")

        self.assertEqual(target.read_bytes(), b"first")
        self.assertEqual([p.name for p in self.tmp.iterdir()], ["thing.toml"])

    @unittest.skipIf(os.name == "nt", "file modes mean nothing on Windows")
    def test_an_owner_only_file_is_owner_only_before_the_rename(self) -> None:
        from pony.atomic import write_atomically

        target = self.tmp / "tokens.json"
        seen: list[int] = []
        real_replace = os.replace

        def _record(source: object, destination: object) -> None:
            seen.append(Path(str(source)).stat().st_mode & 0o777)
            real_replace(str(source), str(destination))

        with unittest.mock.patch("pony.atomic.os.replace", _record):
            write_atomically(target, b"{}", suffix=".json", owner_only=True)

        self.assertEqual(seen, [0o600])
        self.assertEqual(target.stat().st_mode & 0o777, 0o600)


class UidFilenameTest(unittest.TestCase):
    """One encoding for a UID, the same one a calendar name gets.

    `safe_component` and `_uid_to_filename` percent-encoded a name eight
    lines apart, and only the first had the reserved-stem and
    empty-name guards -- so a UID of ``NUL`` wrote to the null device on
    Windows.  The UID encoder also cut its prefix by character count,
    which can split a ``%XX`` escape in half: what comes back off disk
    is then a UID no index row has, and the next sync sees the event as
    one only the server knows about.
    """

    def test_a_reserved_stem_is_escaped_and_round_trips(self) -> None:
        from pony.calendar.storage import _filename_to_uid, _uid_to_filename

        name = _uid_to_filename("NUL")
        self.assertNotEqual(name, "NUL.ics")
        self.assertEqual(_filename_to_uid(name), "NUL")

    def test_an_empty_uid_is_refused_rather_than_written(self) -> None:
        from pony.calendar.storage import _uid_to_filename

        with self.assertRaises(MirrorError):
            _uid_to_filename("")

    def test_a_truncated_prefix_is_cut_between_characters(self) -> None:
        from pony.calendar.storage import _MAX_ENCODED_UID_LEN, _uid_to_filename

        # Long enough that the prefix is cut, with multi-byte characters
        # straddling the cut at every alignment.
        for padding in range(_MAX_ENCODED_UID_LEN - 6, _MAX_ENCODED_UID_LEN):
            uid = "a" * padding + "€€€€"
            with self.subTest(padding=padding):
                stem = _uid_to_filename(uid).removesuffix(".ics")
                prefix = stem.rsplit("-", 1)[0]
                # Decodable, and what it decodes to is the start of the
                # UID rather than something else that happens to parse.
                decoded = urllib.parse.unquote(prefix, errors="strict")
                self.assertTrue(uid.startswith(decoded))

    def test_an_ordinary_uid_round_trips(self) -> None:
        from pony.calendar.storage import _filename_to_uid, _uid_to_filename

        for uid in ("plain@example.com", "with space@example.com", "a/b@example.com"):
            with self.subTest(uid=uid):
                self.assertEqual(_filename_to_uid(_uid_to_filename(uid)), uid)
