"""Create a private, bounded Codex history snapshot without reading account state."""

from __future__ import annotations

from contextlib import closing, contextmanager
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from typing import Iterator

from .collector import MAX_HISTORY_PROJECTION_BYTES, _transcript_thread_ids, _validate_history_projection

MAX_TRANSCRIPT_FILES = 5_000
MAX_TRANSCRIPT_BYTES = 512 * 1024 * 1024
MAX_TRANSCRIPT_FILE_BYTES = 128 * 1024 * 1024
_COPY_CHUNK = 1024 * 1024


class MirrorPreparationError(RuntimeError):
    """Content-free preparation failure suitable for reporting to Watchkeeper."""

    def __init__(self, kind: str):
        if kind not in {"protocol", "unavailable"}:
            kind = "protocol"
        super().__init__(kind)
        self.kind = kind


def _fail(kind: str):
    raise MirrorPreparationError(kind)


def _fingerprint(info: os.stat_result):
    return (info.st_dev, info.st_ino, info.st_mode, info.st_size, info.st_mtime_ns, info.st_ctime_ns)


def _safe_regular_file(path: Path, *, missing_ok=False):
    try:
        info = path.lstat()
    except FileNotFoundError:
        if missing_ok:
            return None
        _fail("unavailable")
    except OSError:
        _fail("unavailable")
    if stat.S_ISLNK(info.st_mode) or not stat.S_ISREG(info.st_mode):
        _fail("protocol")
    return info


def _inventory(source: Path):
    files = []
    total_bytes = 0
    for dirname in ("sessions", "archived_sessions"):
        directory = source / dirname
        try:
            info = directory.lstat()
        except FileNotFoundError:
            continue
        except OSError:
            _fail("unavailable")
        if stat.S_ISLNK(info.st_mode) or not stat.S_ISDIR(info.st_mode):
            _fail("protocol")

        def walk_error(_error):
            _fail("unavailable")

        for current, directories, names in os.walk(directory, topdown=True, followlinks=False, onerror=walk_error):
            current_path = Path(current)
            for name in directories:
                try:
                    child_info = (current_path / name).lstat()
                except OSError:
                    _fail("unavailable")
                if stat.S_ISLNK(child_info.st_mode) or not stat.S_ISDIR(child_info.st_mode):
                    _fail("protocol")
            for name in names:
                item = current_path / name
                try:
                    item_info = item.lstat()
                except OSError:
                    _fail("unavailable")
                if stat.S_ISLNK(item_info.st_mode) or not stat.S_ISREG(item_info.st_mode):
                    _fail("protocol")
                if item.suffix != ".jsonl":
                    continue
                if item_info.st_size > MAX_TRANSCRIPT_FILE_BYTES:
                    _fail("protocol")
                total_bytes += item_info.st_size
                if total_bytes > MAX_TRANSCRIPT_BYTES or len(files) >= MAX_TRANSCRIPT_FILES:
                    _fail("protocol")
                files.append((item, item.relative_to(source), _fingerprint(item_info)))
    if not files:
        _fail("protocol")
    return tuple(sorted(files, key=lambda entry: str(entry[1])))


def _copy_file_checked(source: Path, destination: Path, expected_fingerprint):
    nofollow = getattr(os, "O_NOFOLLOW", 0)
    source_fd = destination_fd = None
    try:
        source_fd = os.open(source, os.O_RDONLY | nofollow)
        destination_fd = os.open(destination, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
    except OSError:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)
        _fail("unavailable")
    try:
        before = os.fstat(source_fd)
        if not stat.S_ISREG(before.st_mode) or _fingerprint(before) != expected_fingerprint:
            _fail("unavailable")
        with os.fdopen(source_fd, "rb", closefd=False) as input_stream, \
                os.fdopen(destination_fd, "wb", closefd=False) as output_stream:
            remaining = expected_fingerprint[3]
            while remaining:
                chunk = input_stream.read(min(_COPY_CHUNK, remaining))
                if not chunk:
                    _fail("unavailable")
                output_stream.write(chunk)
                remaining -= len(chunk)
            if input_stream.read(1):
                _fail("unavailable")
            output_stream.flush()
            os.fsync(output_stream.fileno())
        after = os.fstat(source_fd)
        if _fingerprint(after) != expected_fingerprint:
            _fail("unavailable")
    finally:
        if source_fd is not None:
            os.close(source_fd)
        if destination_fd is not None:
            os.close(destination_fd)


def _copy_transcript(entry, destination: Path):
    source, relative, expected = entry
    destination_path = destination / relative
    destination_path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    _copy_file_checked(source, destination_path, expected)


def _copy_projection(source: Path, destination: Path, selected_thread_ids: set[str]):
    projection = source / "thread_history_1.sqlite"
    db_info = _safe_regular_file(projection, missing_ok=True)
    if db_info is None:
        if any(os.path.lexists(Path(str(projection) + suffix))
               for suffix in ("-wal", "-shm", "-journal")):
            _fail("protocol")
        return False
    if db_info.st_size > MAX_HISTORY_PROJECTION_BYTES:
        _fail("protocol")

    wal_path = Path(str(projection) + "-wal")
    shm_path = Path(str(projection) + "-shm")
    journal_path = Path(str(projection) + "-journal")
    wal_info = _safe_regular_file(wal_path, missing_ok=True)
    # SQLite may consult its WAL/SHM pair for a consistent read-only backup.
    # Neither sidecar is copied to the final mirror.
    _safe_regular_file(shm_path, missing_ok=True)
    if os.path.lexists(journal_path):
        _fail("unavailable")
    if wal_info is not None and db_info.st_size + wal_info.st_size > MAX_HISTORY_PROJECTION_BYTES:
        _fail("protocol")

    staging = destination / ".history-staging"
    staging.mkdir(mode=0o700)
    stage_db = staging / "thread_history_1.sqlite"
    db_fingerprint = _fingerprint(db_info)
    wal_fingerprint = _fingerprint(wal_info) if wal_info is not None else None
    _copy_file_checked(projection, stage_db, db_fingerprint)
    if wal_info is not None:
        _copy_file_checked(wal_path, Path(str(stage_db) + "-wal"), wal_fingerprint)
    elif _safe_regular_file(wal_path, missing_ok=True) is not None:
        _fail("unavailable")

    # Check both durable source files after staging. This catches an active
    # writer changing the pair while it was copied.
    if _fingerprint(_safe_regular_file(projection)) != db_fingerprint:
        _fail("unavailable")
    current_wal = _safe_regular_file(wal_path, missing_ok=True)
    if (_fingerprint(current_wal) if current_wal is not None else None) != wal_fingerprint:
        _fail("unavailable")

    destination_path = destination / "thread_history_1.sqlite"
    stage_uri = stage_db.resolve().as_uri() + "?mode=ro"
    try:
        with closing(sqlite3.connect(stage_uri, uri=True, timeout=2)) as source_db, \
                closing(sqlite3.connect(destination_path, timeout=2)) as destination_db:
            source_db.execute("PRAGMA query_only=ON")
            source_db.backup(destination_db, pages=256, sleep=0.05)
    except sqlite3.Error:
        _fail("protocol")
    finally:
        shutil.rmtree(staging, ignore_errors=True)
    try:
        _validate_history_projection(destination_path, selected_thread_ids)
    except (OSError, ValueError):
        _fail("protocol")
    destination_path.chmod(0o600)
    return True


@contextmanager
def prepare_codex_mirror(source_location: str | os.PathLike[str]) -> Iterator[Path]:
    """Yield a private temporary mirror made only from selected Codex history.

    No home discovery occurs: source_location must be supplied explicitly.
    Configuration, authentication, sessions outside the two selected transcript
    directories, and the general state database are never opened or copied.
    """
    source = Path(source_location)
    try:
        source_info = source.lstat()
    except OSError:
        _fail("unavailable")
    if stat.S_ISLNK(source_info.st_mode) or not stat.S_ISDIR(source_info.st_mode):
        _fail("protocol")
    before = _inventory(source)
    with tempfile.TemporaryDirectory(prefix="watchkeeper-codex-mirror-") as temp:
        mirror = Path(temp)
        for entry in before:
            _copy_transcript(entry, mirror)
        selected_thread_ids = _transcript_thread_ids(mirror)
        _copy_projection(source, mirror, selected_thread_ids)
        after = _inventory(source)
        if after != before:
            _fail("unavailable")
        marker = mirror / ".watchkeeper-reader-state"
        marker.write_text(
            '{"format":"watchkeeper-codex-reader-state-v1","credentials_excluded":true,"transcripts_only":true}',
            encoding="utf-8")
        marker.chmod(0o600)
        yield mirror
