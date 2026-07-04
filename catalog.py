"""SQLite catalog: files, thumbnail cache, edit stacks.

Concurrency model (PLAN.md): WAL mode, exactly one writer thread that owns
the write connection and drains a queue — callers get a Future. Reads run on
the calling thread through thread-local read-only connections; WAL keeps them
non-blocking against the writer. The UI thread only ever does fire-and-forget
writes and tiny point reads.
"""
from __future__ import annotations

import os
import queue
import sqlite3
import sys
import threading
import time
from concurrent.futures import Future
from dataclasses import dataclass

SCHEMA = """
CREATE TABLE IF NOT EXISTS files(
    id          INTEGER PRIMARY KEY,
    path        TEXT UNIQUE NOT NULL,
    mtime       REAL NOT NULL,
    size        INTEGER NOT NULL,
    width       INTEGER NOT NULL DEFAULT 0,
    height      INTEGER NOT NULL DEFAULT 0,
    orientation INTEGER NOT NULL DEFAULT 1,
    capture_dt  TEXT,
    rating      INTEGER NOT NULL DEFAULT 0,
    flag        INTEGER NOT NULL DEFAULT 0    -- 0 none, 1 pick, -1 reject
);
CREATE TABLE IF NOT EXISTS thumbs(
    file_id INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    small   BLOB,                             -- ~256 px JPEG
    large   BLOB,                             -- ~1024 px JPEG
    edited  INTEGER NOT NULL DEFAULT 0        -- rendered through the edit stack?
);
CREATE TABLE IF NOT EXISTS edits(
    file_id    INTEGER PRIMARY KEY REFERENCES files(id) ON DELETE CASCADE,
    stack      TEXT NOT NULL,                 -- edit stack JSON, source of truth
    updated_at TEXT NOT NULL
);
"""


def default_db_path() -> str:
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA",
                              os.path.expanduser("~/AppData/Local"))
    elif sys.platform == "darwin":
        base = os.path.expanduser("~/Library/Application Support")
    else:
        base = os.environ.get("XDG_DATA_HOME",
                              os.path.expanduser("~/.local/share"))
    d = os.path.join(base, "photoflow")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "catalog.db")


@dataclass
class FileRecord:
    id: int
    path: str
    mtime: float
    size: int
    width: int
    height: int
    orientation: int
    capture_dt: str | None
    rating: int
    flag: int
    stack_json: str | None
    has_thumb: bool
    changed: bool  # mtime/size differed from the catalog → caches were dropped


class Catalog:
    def __init__(self, db_path: str | None = None):
        self.db_path = db_path or default_db_path()
        self._queue: queue.SimpleQueue = queue.SimpleQueue()
        self._local = threading.local()
        self._closed = False
        self._writer = threading.Thread(target=self._writer_loop,
                                        name="catalog-writer", daemon=True)
        self._ready = threading.Event()
        self._writer.start()
        self._ready.wait()

    # -- connections --------------------------------------------------------

    def _connect(self, readonly: bool) -> sqlite3.Connection:
        conn = sqlite3.connect(self.db_path, timeout=30)
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA foreign_keys=ON")
        if readonly:
            conn.execute("PRAGMA query_only=ON")
        return conn

    def _read_conn(self) -> sqlite3.Connection:
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = self._local.conn = self._connect(readonly=True)
        return conn

    # -- writer thread -------------------------------------------------------

    def _writer_loop(self) -> None:
        conn = self._connect(readonly=False)
        conn.executescript(SCHEMA)
        conn.commit()
        self._ready.set()
        while True:
            item = self._queue.get()
            if item is None:
                break
            # Batch everything already queued into one transaction.
            batch = [item]
            while True:
                try:
                    nxt = self._queue.get_nowait()
                except queue.Empty:
                    break
                if nxt is None:
                    self._queue.put(None)
                    break
                batch.append(nxt)
            for fn, fut in batch:
                try:
                    result = fn(conn)
                except Exception as e:
                    conn.rollback()
                    fut.set_exception(e)
                else:
                    fut.set_result(result)
            conn.commit()
        conn.commit()
        conn.close()

    def _write(self, fn) -> Future:
        fut: Future = Future()
        if self._closed:
            fut.set_exception(RuntimeError("catalog closed"))
            return fut
        self._queue.put((fn, fut))
        return fut

    def close(self) -> None:
        if not self._closed:
            self._closed = True
            self._queue.put(None)
            self._writer.join(timeout=10)

    # -- ingest (folder scan) -------------------------------------------------

    def ingest(self, entries: list[tuple[str, float, int]]) -> Future:
        """Upsert (path, mtime, size) rows; a changed file drops its thumbs.
        Resolves to list[FileRecord] in input order."""
        def op(conn: sqlite3.Connection) -> list[FileRecord]:
            out = []
            for path, mtime, size in entries:
                row = conn.execute(
                    "SELECT id, mtime, size, width, height, orientation,"
                    " capture_dt, rating, flag FROM files WHERE path=?",
                    (path,)).fetchone()
                changed = False
                if row is None:
                    fid = conn.execute(
                        "INSERT INTO files(path, mtime, size) VALUES(?,?,?)",
                        (path, mtime, size)).lastrowid
                    width = height = 0
                    orientation, capture_dt, rating, flag = 1, None, 0, 0
                else:
                    (fid, old_mtime, old_size, width, height, orientation,
                     capture_dt, rating, flag) = row
                    if abs(old_mtime - mtime) > 1e-6 or old_size != size:
                        changed = True
                        conn.execute(
                            "UPDATE files SET mtime=?, size=?, width=0,"
                            " height=0, orientation=1, capture_dt=NULL"
                            " WHERE id=?", (mtime, size, fid))
                        conn.execute("DELETE FROM thumbs WHERE file_id=?", (fid,))
                        width = height = 0
                        orientation, capture_dt = 1, None
                stack_json = None
                srow = conn.execute(
                    "SELECT stack FROM edits WHERE file_id=?", (fid,)).fetchone()
                if srow:
                    stack_json = srow[0]
                has_thumb = not changed and conn.execute(
                    "SELECT 1 FROM thumbs WHERE file_id=? AND small IS NOT NULL",
                    (fid,)).fetchone() is not None
                out.append(FileRecord(fid, path, mtime, size, width, height,
                                      orientation, capture_dt, rating, flag,
                                      stack_json, has_thumb, changed))
            return out
        return self._write(op)

    # -- metadata / culling ----------------------------------------------------

    def set_meta(self, fid: int, width: int, height: int,
                 orientation: int, capture_dt: str | None) -> Future:
        return self._write(lambda c: c.execute(
            "UPDATE files SET width=?, height=?, orientation=?,"
            " capture_dt=COALESCE(?, capture_dt) WHERE id=?",
            (width, height, orientation, capture_dt, fid)))

    def set_rating(self, fid: int, rating: int) -> Future:
        return self._write(lambda c: c.execute(
            "UPDATE files SET rating=? WHERE id=?", (rating, fid)))

    def set_flag(self, fid: int, flag: int) -> Future:
        return self._write(lambda c: c.execute(
            "UPDATE files SET flag=? WHERE id=?", (flag, fid)))

    def remove_files(self, fids: list[int]) -> Future:
        """Drop catalog rows (thumbs/edits cascade). Used after trashing."""
        return self._write(lambda c: c.executemany(
            "DELETE FROM files WHERE id=?", [(f,) for f in fids]))

    def drop_thumbs(self, fids: list[int]) -> Future:
        """Invalidate cached thumbnails (force-rescan path)."""
        return self._write(lambda c: c.executemany(
            "DELETE FROM thumbs WHERE file_id=?", [(f,) for f in fids]))

    def clear_all(self) -> Future:
        """Wipe the entire catalog: every edit stack, rating, flag and cached
        thumbnail. Source image files are never touched."""
        def op(conn: sqlite3.Connection):
            conn.execute("DELETE FROM edits")
            conn.execute("DELETE FROM thumbs")
            conn.execute("DELETE FROM files")
            conn.commit()
            conn.execute("VACUUM")  # needs autocommit, hence the commit above
        return self._write(op)

    # -- edit stacks -------------------------------------------------------------

    def set_stack(self, fid: int, stack_json: str | None) -> Future:
        def op(conn):
            if stack_json is None:
                conn.execute("DELETE FROM edits WHERE file_id=?", (fid,))
            else:
                conn.execute(
                    "INSERT INTO edits(file_id, stack, updated_at) VALUES(?,?,?)"
                    " ON CONFLICT(file_id) DO UPDATE SET stack=excluded.stack,"
                    " updated_at=excluded.updated_at",
                    (fid, stack_json,
                     time.strftime("%Y-%m-%d %H:%M:%S")))
        return self._write(op)

    def get_stack(self, fid: int) -> str | None:
        row = self._read_conn().execute(
            "SELECT stack FROM edits WHERE file_id=?", (fid,)).fetchone()
        return row[0] if row else None

    # -- thumbnail cache ------------------------------------------------------------

    def put_thumbs(self, fid: int, small: bytes, large: bytes,
                   edited: bool) -> Future:
        return self._write(lambda c: c.execute(
            "INSERT INTO thumbs(file_id, small, large, edited) VALUES(?,?,?,?)"
            " ON CONFLICT(file_id) DO UPDATE SET small=excluded.small,"
            " large=excluded.large, edited=excluded.edited",
            (fid, small, large, int(edited))))

    def get_thumb_small(self, fid: int) -> tuple[bytes, bool] | None:
        row = self._read_conn().execute(
            "SELECT small, edited FROM thumbs WHERE file_id=?", (fid,)).fetchone()
        return (row[0], bool(row[1])) if row and row[0] else None

    def get_thumb_large(self, fid: int) -> tuple[bytes, bool] | None:
        row = self._read_conn().execute(
            "SELECT large, edited FROM thumbs WHERE file_id=?", (fid,)).fetchone()
        return (row[0], bool(row[1])) if row and row[0] else None
