"""SQLite storage.

Consistency units
-----------------
*The active dataset* is the union of samples and batches ingested so far. Each
batch number identifies one physical shipment and carries a monotonically
increasing ``version``. Batches are never mutated in place: corrections insert
a new version and mark the old row ``superseded``.

*A trace* is a query snapshot. On acceptance the current intake id and every
live batch version are recorded. The result rows and the ``complete`` flag are
written in one transaction, so a reader never observes a partially published
result; a failure leaves zero result rows.

*Invalidation*: a correction marks every completed trace that pinned the
superseded version. Reading such a trace returns 409 instead of stale data.
"""
from __future__ import annotations

import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Iterator

UTC = timezone.utc

SCHEMA = """
PRAGMA journal_mode=WAL;
PRAGMA foreign_keys=ON;

CREATE TABLE IF NOT EXISTS intakes (
    id         INTEGER PRIMARY KEY AUTOINCREMENT,
    status     TEXT NOT NULL CHECK (status IN ('active', 'archived')),
    created_at TEXT NOT NULL
);

CREATE UNIQUE INDEX IF NOT EXISTS intakes_one_active
    ON intakes(status) WHERE status = 'active';

CREATE TABLE IF NOT EXISTS samples (
    intake_id    INTEGER NOT NULL REFERENCES intakes(id) ON DELETE CASCADE,
    sample_id    TEXT NOT NULL,
    contaminated INTEGER NOT NULL DEFAULT 0 CHECK (contaminated IN (0, 1)),
    PRIMARY KEY (intake_id, sample_id)
);

CREATE TABLE IF NOT EXISTS batches (
    id          INTEGER PRIMARY KEY AUTOINCREMENT,
    intake_id   INTEGER NOT NULL REFERENCES intakes(id) ON DELETE CASCADE,
    batch_no    TEXT NOT NULL,
    version     INTEGER NOT NULL CHECK (version >= 1),
    origin      TEXT NOT NULL,
    destination TEXT NOT NULL,
    departed_at TEXT NOT NULL,
    arrived_at  TEXT NOT NULL,
    created_at  TEXT NOT NULL,
    superseded  INTEGER NOT NULL DEFAULT 0 CHECK (superseded IN (0, 1)),
    UNIQUE (intake_id, batch_no, version)
);

CREATE UNIQUE INDEX IF NOT EXISTS batches_one_live
    ON batches(intake_id, batch_no) WHERE superseded = 0;

CREATE TABLE IF NOT EXISTS sample_batches (
    intake_id INTEGER NOT NULL REFERENCES intakes(id) ON DELETE CASCADE,
    sample_id TEXT NOT NULL,
    batch_no  TEXT NOT NULL,
    PRIMARY KEY (intake_id, sample_id, batch_no)
);

CREATE TABLE IF NOT EXISTS traces (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    intake_id    INTEGER NOT NULL REFERENCES intakes(id),
    status       TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'failed')),
    accepted_at  TEXT NOT NULL,
    completed_at TEXT,
    error        TEXT,
    sources_json TEXT NOT NULL,
    pinned_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS trace_results (
    trace_id         INTEGER NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    rank             INTEGER NOT NULL,
    sample_id        TEXT NOT NULL,
    first_arrival    TEXT NOT NULL,
    quarantine_until TEXT NOT NULL,
    path_json        TEXT NOT NULL,
    PRIMARY KEY (trace_id, sample_id)
);

CREATE TABLE IF NOT EXISTS trace_invalidations (
    trace_id       INTEGER NOT NULL REFERENCES traces(id) ON DELETE CASCADE,
    batch_no       TEXT NOT NULL,
    version        INTEGER NOT NULL,
    invalidated_at TEXT NOT NULL,
    reason         TEXT NOT NULL,
    PRIMARY KEY (trace_id, batch_no, version)
);

CREATE TABLE IF NOT EXISTS request_log (
    id      INTEGER PRIMARY KEY AUTOINCREMENT,
    kind    TEXT NOT NULL,
    at      TEXT NOT NULL,
    payload TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_queries (
    id           INTEGER PRIMARY KEY AUTOINCREMENT,
    intake_id    INTEGER NOT NULL REFERENCES intakes(id),
    status       TEXT NOT NULL CHECK (status IN ('pending', 'complete', 'failed')),
    site         TEXT NOT NULL,
    as_of        TEXT NOT NULL,
    accepted_at  TEXT NOT NULL,
    completed_at TEXT,
    error        TEXT,
    assoc_json   TEXT NOT NULL,
    pinned_json  TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS inventory_results (
    query_id       INTEGER NOT NULL
                   REFERENCES inventory_queries(id) ON DELETE CASCADE,
    kind           TEXT NOT NULL CHECK (kind IN ('in_stock', 'conflict')),
    rank           INTEGER NOT NULL,
    sample_id      TEXT NOT NULL,
    in_stock_since TEXT,
    in_stock_until TEXT,
    support_json   TEXT,
    conflicts_json TEXT,
    PRIMARY KEY (query_id, kind, sample_id)
);

CREATE TABLE IF NOT EXISTS inventory_invalidations (
    query_id       INTEGER NOT NULL
                   REFERENCES inventory_queries(id) ON DELETE CASCADE,
    seq            INTEGER NOT NULL,
    kind           TEXT NOT NULL CHECK (kind IN ('batch', 'association')),
    batch_no       TEXT,
    version        INTEGER,
    sample_id      TEXT,
    invalidated_at TEXT NOT NULL,
    reason         TEXT NOT NULL,
    PRIMARY KEY (query_id, seq)
);

CREATE INDEX IF NOT EXISTS inventory_results_by_kind
    ON inventory_results(query_id, kind, rank);
"""

_SCHEMA_LOCK = threading.Lock()


def utcnow() -> datetime:
    return datetime.now(UTC)


def iso(dt: datetime) -> str:
    """Fixed-width UTC string; lexicographic order equals chronological order."""
    return dt.astimezone(UTC).isoformat(timespec="microseconds")


def parse_iso(value: str) -> datetime:
    return datetime.fromisoformat(value)


class StoreError(Exception):
    """Base class for expected storage failures."""


class ConflictError(StoreError):
    """The request conflicts with current state (HTTP 409)."""


class NotFoundError(StoreError):
    """Unknown record (HTTP 404)."""


class InvalidReferenceError(StoreError):
    """Payload refers to something that is not in the dataset (HTTP 422)."""


class Store:
    def __init__(self, path: str | Path) -> None:
        self.path = str(path)
        self._lock = threading.RLock()
        self._ready = False
        self._memory_conn: sqlite3.Connection | None = None
        if self.path != ":memory:":
            Path(self.path).expanduser().resolve().parent.mkdir(
                parents=True, exist_ok=True
            )

    def _ensure_schema(self) -> None:
        if self._ready:
            return
        with _SCHEMA_LOCK:
            if self._ready:
                return
            conn = self._new_connection()
            try:
                conn.executescript(SCHEMA)
            finally:
                if self.path == ":memory:":
                    self._memory_conn = conn
                else:
                    conn.close()
            self._ready = True

    def _new_connection(self) -> sqlite3.Connection:
        conn = sqlite3.connect(
            self.path,
            timeout=30,
            isolation_level=None,
            check_same_thread=self.path == ":memory:",
        )
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA foreign_keys=ON")
        conn.execute("PRAGMA busy_timeout=30000")
        return conn

    @contextmanager
    def connection(self) -> Iterator[sqlite3.Connection]:
        self._ensure_schema()
        if self._memory_conn is not None:
            # One connection backs an in-memory database; serialise access.
            with self._lock:
                yield self._memory_conn
            return
        conn = self._new_connection()
        try:
            yield conn
        finally:
            conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        """Serialised write transaction (BEGIN IMMEDIATE)."""
        self._ensure_schema()
        if self._memory_conn is not None:
            with self._lock:
                self._memory_conn.execute("BEGIN IMMEDIATE")
                try:
                    yield self._memory_conn
                except BaseException:
                    self._memory_conn.execute("ROLLBACK")
                    raise
                self._memory_conn.execute("COMMIT")
            return

        conn = self._new_connection()
        try:
            conn.execute("BEGIN IMMEDIATE")
            try:
                yield conn
            except BaseException:
                conn.execute("ROLLBACK")
                raise
            conn.execute("COMMIT")
        finally:
            conn.close()

    def mark_interrupted(self, *, older_than: timedelta = timedelta(minutes=10)) -> int:
        """Fail stale pending traces left by a crashed process.

        Only traces accepted more than ``older_than`` ago are touched. With
        several API workers sharing one database, another worker may legitimately
        be computing a trace right now; the grace period keeps startup recovery
        from failing live work. A pending trace has no published rows, so
        marking it failed never exposes partial data.
        """
        cutoff = iso(utcnow() - older_than)
        with self.transaction() as conn:
            cur = conn.execute(
                "UPDATE traces SET status='failed', completed_at=?,"
                " error=? WHERE status='pending' AND accepted_at<?",
                (iso(utcnow()), "interrupted before completion", cutoff),
            )
            conn.execute(
                "UPDATE inventory_queries SET status='failed', completed_at=?,"
                " error=? WHERE status='pending' AND accepted_at<?",
                (iso(utcnow()), "interrupted before completion", cutoff),
            )
            return cur.rowcount

    # ----------------------------------------------------------------- intake

    @staticmethod
    def _active_intake_id(conn: sqlite3.Connection, *, create: bool) -> int | None:
        row = conn.execute(
            "SELECT id FROM intakes WHERE status='active'"
        ).fetchone()
        if row is not None:
            return int(row["id"])
        if not create:
            return None
        cur = conn.execute(
            "INSERT INTO intakes(status, created_at) VALUES ('active', ?)",
            (iso(utcnow()),),
        )
        return int(cur.lastrowid)

    def ingest(
        self,
        *,
        samples: list[dict[str, Any]],
        batches: list[dict[str, Any]],
        source_sample_ids: list[str],
    ) -> dict[str, Any]:
        """Add samples/batches to the active dataset atomically.

        Identical re-submissions are idempotent. A batch number whose live
        values differ from the submitted values is rejected; it must go through
        :meth:`correct_batch` with an explicit ``seen_version``.

        Raises:
            ConflictError: conflicting batch definition, or unknown source id.
        """
        now = iso(utcnow())
        with self.transaction() as conn:
            intake_id = self._active_intake_id(conn, create=True)
            assert intake_id is not None

            inserted_batches = 0
            for batch in batches:
                live = conn.execute(
                    "SELECT * FROM batches WHERE intake_id=? AND batch_no=?"
                    " AND superseded=0",
                    (intake_id, batch["batch_no"]),
                ).fetchone()
                if live is not None:
                    same = (
                        live["origin"] == batch["origin"]
                        and live["destination"] == batch["destination"]
                        and live["departed_at"] == iso(batch["departed_at"])
                        and live["arrived_at"] == iso(batch["arrived_at"])
                    )
                    if same:
                        continue
                    raise ConflictError(
                        f"batch {batch['batch_no']!r} already exists at version"
                        f" {live['version']} with different values; submit a"
                        " correction with seen_version instead"
                    )
                conn.execute(
                    "INSERT INTO batches(intake_id, batch_no, version, origin,"
                    " destination, departed_at, arrived_at, created_at)"
                    " VALUES (?, ?, 1, ?, ?, ?, ?, ?)",
                    (
                        intake_id,
                        batch["batch_no"],
                        batch["origin"],
                        batch["destination"],
                        iso(batch["departed_at"]),
                        iso(batch["arrived_at"]),
                        now,
                    ),
                )
                inserted_batches += 1

            inserted_samples = 0
            added_associations: list[tuple[str, str]] = []
            for sample in samples:
                missing = [
                    batch_no
                    for batch_no in sample["batch_nos"]
                    if conn.execute(
                        "SELECT 1 FROM batches WHERE intake_id=? AND batch_no=?"
                        " AND superseded=0",
                        (intake_id, batch_no),
                    ).fetchone()
                    is None
                ]
                if missing:
                    raise InvalidReferenceError(
                        f"sample {sample['sample_id']!r} references unknown"
                        f" batch(es): {', '.join(sorted(missing))}"
                    )
                existing = conn.execute(
                    "SELECT contaminated FROM samples WHERE intake_id=?"
                    " AND sample_id=?",
                    (intake_id, sample["sample_id"]),
                ).fetchone()
                if existing is None:
                    conn.execute(
                        "INSERT INTO samples(intake_id, sample_id, contaminated)"
                        " VALUES (?, ?, ?)",
                        (
                            intake_id,
                            sample["sample_id"],
                            int(sample["contaminated"]),
                        ),
                    )
                    inserted_samples += 1
                elif sample["contaminated"] and not existing["contaminated"]:
                    conn.execute(
                        "UPDATE samples SET contaminated=1 WHERE intake_id=?"
                        " AND sample_id=?",
                        (intake_id, sample["sample_id"]),
                    )
                for batch_no in sample["batch_nos"]:
                    cur = conn.execute(
                        "INSERT OR IGNORE INTO sample_batches(intake_id,"
                        " sample_id, batch_no) VALUES (?, ?, ?)",
                        (intake_id, sample["sample_id"], batch_no),
                    )
                    if cur.rowcount:
                        added_associations.append(
                            (sample["sample_id"], batch_no)
                        )
            for sample_id in source_sample_ids:
                updated = conn.execute(
                    "UPDATE samples SET contaminated=1 WHERE intake_id=?"
                    " AND sample_id=?",
                    (intake_id, sample_id),
                ).rowcount
                if not updated:
                    raise InvalidReferenceError(
                        f"source sample {sample_id!r} not found in the dataset"
                    )

            # A newly recorded association changes the basis of every inventory
            # conclusion: even a brand-new sample may belong on the roster the
            # old query published. Identical re-submissions add nothing and
            # therefore invalidate nothing.
            if added_associations:
                self._invalidate_associations_for(
                    conn, intake_id, added_associations, now
                )

            dataset = self._dataset(conn, intake_id)
            return {
                "intake_id": intake_id,
                "created_at": dataset["created_at"],
                "sample_count": len(dataset["samples"]),
                "batch_count": len(dataset["batches"]),
                "source_sample_ids": dataset["source_sample_ids"],
                "inserted_batches": inserted_batches,
                "inserted_samples": inserted_samples,
            }

    # ------------------------------------------------- inventory invalidation

    @staticmethod
    def _inventory_seq(conn: sqlite3.Connection, query_id: int) -> int:
        row = conn.execute(
            "SELECT COALESCE(MAX(seq), 0) AS seq FROM inventory_invalidations"
            " WHERE query_id=?",
            (query_id,),
        ).fetchone()
        return int(row["seq"]) + 1

    def _mark_inventory(
        self,
        conn: sqlite3.Connection,
        query_id: int,
        now: str,
        *,
        kind: str,
        reason: str,
        batch_no: str | None = None,
        version: int | None = None,
        sample_id: str | None = None,
    ) -> None:
        conn.execute(
            "INSERT INTO inventory_invalidations(query_id, seq, kind, batch_no,"
            " version, sample_id, invalidated_at, reason)"
            " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
            (
                query_id,
                self._inventory_seq(conn, query_id),
                kind,
                batch_no,
                version,
                sample_id,
                now,
                reason,
            ),
        )

    def _invalidate_associations_for(
        self,
        conn: sqlite3.Connection,
        intake_id: int,
        added: list[tuple[str, str]],
        now: str,
    ) -> list[int]:
        """Invalidate completed inventory queries when associations are added.

        Only genuinely new ``(sample_id, batch_no)`` links reach this method;
        an identical re-submission records nothing and therefore invalidates
        nothing. Every completed check in the dataset is affected: the roster
        is a list of *all* samples at a station, so even a sample that is new to
        the dataset or rides a batch it did not ride before can change it.
        """
        affected = [
            int(row["id"])
            for row in conn.execute(
                "SELECT id FROM inventory_queries"
                " WHERE intake_id=? AND status='complete' ORDER BY id",
                (intake_id,),
            ).fetchall()
        ]
        for query_id in affected:
            for sample_id, batch_no in sorted(added):
                self._mark_inventory(
                    conn,
                    query_id,
                    now,
                    kind="association",
                    reason=(
                        f"association {sample_id!r} -> batch {batch_no!r} was"
                        " recorded after this query was accepted"
                    ),
                    sample_id=sample_id,
                    batch_no=batch_no,
                )
        return affected

    # ------------------------------------------------------------------ reads

    @staticmethod
    def _dataset(
        conn: sqlite3.Connection, intake_id: int
    ) -> dict[str, Any]:
        intake = conn.execute(
            "SELECT created_at FROM intakes WHERE id=?", (intake_id,)
        ).fetchone()
        samples = conn.execute(
            "SELECT sample_id, contaminated FROM samples"
            " WHERE intake_id=? ORDER BY sample_id",
            (intake_id,),
        ).fetchall()
        links = conn.execute(
            "SELECT sample_id, batch_no FROM sample_batches"
            " WHERE intake_id=? ORDER BY sample_id, batch_no",
            (intake_id,),
        ).fetchall()
        batches = conn.execute(
            "SELECT batch_no, version, origin, destination, departed_at,"
            " arrived_at FROM batches WHERE intake_id=? AND superseded=0"
            " ORDER BY batch_no",
            (intake_id,),
        ).fetchall()
        by_sample: dict[str, list[str]] = {}
        for link in links:
            by_sample.setdefault(link["sample_id"], []).append(link["batch_no"])
        return {
            "intake_id": intake_id,
            "created_at": parse_iso(intake["created_at"]),
            "samples": [
                {
                    "sample_id": row["sample_id"],
                    "contaminated": bool(row["contaminated"]),
                    "batch_nos": by_sample.get(row["sample_id"], []),
                }
                for row in samples
            ],
            "batches": [dict(row) for row in batches],
            "source_sample_ids": [
                row["sample_id"] for row in samples if row["contaminated"]
            ],
        }

    def dataset(self) -> dict[str, Any] | None:
        with self.connection() as conn:
            intake = conn.execute(
                "SELECT id FROM intakes WHERE status='active'"
            ).fetchone()
            if intake is None:
                return None
            return self._dataset(conn, int(intake["id"]))

    def snapshot(self) -> dict[str, Any] | None:
        """One consistent read transaction over the active dataset.

        Returns the batches with the samples riding on them, plus the pinned
        version of every batch. ``None`` when no dataset exists yet. Used for
        read-only inspection; ``accept_trace`` takes its own snapshot inside
        the write transaction that pins it.
        """
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                return self._snapshot_in(conn)
            finally:
                conn.execute("COMMIT")

    # ----------------------------------------------------------- corrections

    def correct_batch(
        self,
        *,
        batch_no: str,
        seen_version: int,
        origin: str,
        destination: str,
        departed_at: datetime,
        arrived_at: datetime,
    ) -> dict[str, Any]:
        """Insert a replacement version and invalidate dependent results.

        Raises:
            NotFoundError: batch unknown in the active dataset.
            ConflictError: stale ``seen_version`` or a no-op correction.
        """
        now = iso(utcnow())
        with self.transaction() as conn:
            intake_id = self._active_intake_id(conn, create=False)
            if intake_id is None:
                raise NotFoundError("no active dataset")
            current = conn.execute(
                "SELECT * FROM batches WHERE intake_id=? AND batch_no=?"
                " AND superseded=0",
                (intake_id, batch_no),
            ).fetchone()
            if current is None:
                raise NotFoundError(f"batch {batch_no!r} not found")
            version = int(current["version"])
            if version != seen_version:
                raise ConflictError(
                    f"stale version: batch {batch_no!r} is at version {version},"
                    f" client saw {seen_version}"
                )
            if (
                current["origin"] == origin
                and current["destination"] == destination
                and current["departed_at"] == iso(departed_at)
                and current["arrived_at"] == iso(arrived_at)
            ):
                raise ConflictError(
                    "correction does not change the batch; nothing was replaced"
                )

            conn.execute(
                "UPDATE batches SET superseded=1 WHERE id=?",
                (int(current["id"]),),
            )
            new_version = version + 1
            conn.execute(
                "INSERT INTO batches(intake_id, batch_no, version, origin,"
                " destination, departed_at, arrived_at, created_at, superseded)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?, 0)",
                (
                    intake_id,
                    batch_no,
                    new_version,
                    origin,
                    destination,
                    iso(departed_at),
                    iso(arrived_at),
                    now,
                ),
            )

            rows = conn.execute(
                "SELECT t.id FROM traces t, json_each(t.pinned_json) e"
                " WHERE t.intake_id=? AND t.status='complete'"
                " AND e.key=? AND CAST(e.value AS INTEGER)=?",
                (intake_id, batch_no, version),
            ).fetchall()
            invalidated = sorted(int(row["id"]) for row in rows)
            reason = (
                f"batch {batch_no!r} version {version} was superseded by"
                f" version {new_version}"
            )
            for trace_id in invalidated:
                conn.execute(
                    "INSERT OR IGNORE INTO trace_invalidations(trace_id,"
                    " batch_no, version, invalidated_at, reason)"
                    " VALUES (?, ?, ?, ?, ?)",
                    (trace_id, batch_no, version, now, reason),
                )

            inventory_rows = conn.execute(
                "SELECT q.id FROM inventory_queries q, json_each(q.pinned_json) e"
                " WHERE q.intake_id=? AND q.status='complete'"
                " AND e.key=? AND CAST(e.value AS INTEGER)=?",
                (intake_id, batch_no, version),
            ).fetchall()
            invalidated_inventory = sorted(
                int(row["id"]) for row in inventory_rows
            )
            for query_id in invalidated_inventory:
                self._mark_inventory(
                    conn,
                    query_id,
                    now,
                    kind="batch",
                    reason=reason,
                    batch_no=batch_no,
                    version=version,
                )
            return {
                "batch_no": batch_no,
                "previous_version": version,
                "version": new_version,
                "origin": origin,
                "destination": destination,
                "departed_at": departed_at,
                "arrived_at": arrived_at,
                "invalidated_trace_ids": invalidated,
                "invalidated_inventory_query_ids": invalidated_inventory,
            }

    # ---------------------------------------------------------------- traces

    def accept_trace(
        self,
        *,
        sources: list[str] | None,
        intake_id: int | None = None,
    ) -> tuple[int, dict[str, Any], list[str]]:
        """Atomically snapshot the dataset and pin the trace to it.

        Reading the snapshot and inserting the ``pending`` trace happen in one
        ``BEGIN IMMEDIATE`` transaction, so a concurrent intake or correction
        can never land between "what we saw" and "what we pinned".

        Args:
            sources: explicit source sample ids, or ``None`` to use every
                sample flagged contaminated in the snapshot.
            intake_id: retained for callers that already hold a snapshot; when
                given and the active intake differs, the request is rejected.

        Returns:
            ``(trace_id, snapshot, sources)`` where snapshot is the read-only
            data the computation must use.

        Raises:
            NotFoundError: no active dataset.
            InvalidReferenceError: an explicit source sample is unknown.
            ConflictError: the intake changed since the caller read it.
        """
        with self.transaction() as conn:
            snapshot = self._snapshot_in(conn)
            if snapshot is None:
                raise NotFoundError("no dataset")
            if intake_id is not None and intake_id != snapshot["intake_id"]:
                raise ConflictError("the dataset was replaced; re-run the trace")

            if sources is None:
                resolved = snapshot["source_sample_ids"]
            else:
                unknown = sorted(set(sources) - set(snapshot["all_sample_ids"]))
                if unknown:
                    raise InvalidReferenceError(
                        f"unknown source sample(s): {', '.join(unknown)}"
                    )
                resolved = list(sources)

            cur = conn.execute(
                "INSERT INTO traces(intake_id, status, accepted_at,"
                " sources_json, pinned_json) VALUES (?, 'pending', ?, ?, ?)",
                (
                    snapshot["intake_id"],
                    iso(utcnow()),
                    json.dumps(resolved),
                    json.dumps(snapshot["pinned"], sort_keys=True),
                ),
            )
            return int(cur.lastrowid), snapshot, resolved

    @staticmethod
    def _snapshot_in(conn: sqlite3.Connection) -> dict[str, Any] | None:
        intake = conn.execute(
            "SELECT id, created_at FROM intakes WHERE status='active'"
        ).fetchone()
        if intake is None:
            return None
        intake_id = int(intake["id"])
        samples = conn.execute(
            "SELECT sample_id, contaminated FROM samples"
            " WHERE intake_id=? ORDER BY sample_id",
            (intake_id,),
        ).fetchall()
        links = conn.execute(
            "SELECT sample_id, batch_no FROM sample_batches"
            " WHERE intake_id=? ORDER BY sample_id, batch_no",
            (intake_id,),
        ).fetchall()
        batches = conn.execute(
            "SELECT batch_no, version, origin, destination,"
            " departed_at, arrived_at FROM batches"
            " WHERE intake_id=? AND superseded=0 ORDER BY batch_no",
            (intake_id,),
        ).fetchall()
        riders: dict[str, list[str]] = {}
        for link in links:
            riders.setdefault(link["batch_no"], []).append(link["sample_id"])
        return {
            "intake_id": intake_id,
            "created_at": parse_iso(intake["created_at"]),
            "samples": [dict(row) for row in samples],
            "batches": [
                {**dict(row), "samples": riders.get(row["batch_no"], [])}
                for row in batches
            ],
            "pinned": {row["batch_no"]: int(row["version"]) for row in batches},
            "source_sample_ids": [
                row["sample_id"] for row in samples if row["contaminated"]
            ],
            "all_sample_ids": [row["sample_id"] for row in samples],
        }

    def publish_results(
        self,
        trace_id: int,
        results: list[dict[str, Any]],
        *,
        pinned: dict[str, int],
    ) -> None:
        """Publish every result row and the complete flag in one transaction.

        Refuses to publish if any pinned batch version has since changed.
        """
        now = iso(utcnow())
        with self.transaction() as conn:
            trace = conn.execute(
                "SELECT status FROM traces WHERE id=?", (trace_id,)
            ).fetchone()
            if trace is None:
                raise NotFoundError("trace not found")
            if trace["status"] != "pending":
                raise ConflictError(
                    f"trace {trace_id} is {trace['status']}, not pending"
                )
            live = {
                row["batch_no"]: int(row["version"])
                for row in conn.execute(
                    "SELECT batch_no, version FROM batches"
                    " WHERE intake_id=(SELECT intake_id FROM traces WHERE id=?)"
                    " AND superseded=0",
                    (trace_id,),
                ).fetchall()
            }
            changed = sorted(
                batch_no
                for batch_no, version in pinned.items()
                if live.get(batch_no) != version
            )
            if changed:
                raise ConflictError(
                    "snapshot went stale during computation:"
                    f" {', '.join(changed)}"
                )
            for rank, result in enumerate(results):
                conn.execute(
                    "INSERT INTO trace_results(trace_id, rank, sample_id,"
                    " first_arrival, quarantine_until, path_json)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        trace_id,
                        rank,
                        result["sample_id"],
                        iso(result["first_arrival_at"]),
                        iso(result["quarantine_until"]),
                        json.dumps(
                            [
                                {
                                    "batch_no": leg["batch_no"],
                                    "origin": leg["origin"],
                                    "destination": leg["destination"],
                                    "departed_at": iso(leg["departed_at"]),
                                    "arrived_at": iso(leg["arrived_at"]),
                                    "version": int(leg["version"]),
                                }
                                for leg in result["path"]
                            ]
                        ),
                    ),
                )
            conn.execute(
                "UPDATE traces SET status='complete', completed_at=?, error=NULL"
                " WHERE id=?",
                (now, trace_id),
            )

    def fail_trace(self, trace_id: int, error: str) -> None:
        """Delete any partial rows and record the failure."""
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM trace_results WHERE trace_id=?", (trace_id,)
            )
            conn.execute(
                "UPDATE traces SET status='failed', completed_at=?, error=?"
                " WHERE id=? AND status='pending'",
                (iso(utcnow()), error, trace_id),
            )

    def read_trace(self, trace_id: int) -> dict[str, Any] | None:
        """Read a trace and everything needed to render it, atomically.

        A single read transaction ensures the snapshot header, its result rows
        and its invalidation markers are mutually consistent, so a correction
        committed mid-read cannot produce a half-stale response.

        Returns ``None`` for an unknown trace.
        """
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                trace = conn.execute(
                    "SELECT * FROM traces WHERE id=?", (trace_id,)
                ).fetchone()
                if trace is None:
                    return None
                results = conn.execute(
                    "SELECT * FROM trace_results WHERE trace_id=? ORDER BY rank",
                    (trace_id,),
                ).fetchall()
                invalidations = conn.execute(
                    "SELECT * FROM trace_invalidations WHERE trace_id=?"
                    " ORDER BY invalidated_at, batch_no",
                    (trace_id,),
                ).fetchall()
                pinned = json.loads(trace["pinned_json"])
                pinned_rows = conn.execute(
                    "SELECT batch_no, version, origin, destination, departed_at,"
                    " arrived_at FROM batches WHERE intake_id=?"
                    " ORDER BY batch_no",
                    (int(trace["intake_id"]),),
                ).fetchall()
                return {
                    "trace": trace,
                    "results": results,
                    "invalidations": invalidations,
                    "pinned": pinned,
                    "batches": [
                        {
                            "batch_no": row["batch_no"],
                            "version": int(row["version"]),
                            "origin": row["origin"],
                            "destination": row["destination"],
                            "departed_at": parse_iso(row["departed_at"]),
                            "arrived_at": parse_iso(row["arrived_at"]),
                        }
                        for row in pinned_rows
                        if pinned.get(row["batch_no"]) == int(row["version"])
                    ],
                }
            finally:
                conn.execute("COMMIT")

    def trace(self, trace_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM traces WHERE id=?", (trace_id,)
            ).fetchone()

    def trace_results(self, trace_id: int) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM trace_results WHERE trace_id=? ORDER BY rank",
                (trace_id,),
            ).fetchall()

    def invalidations(self, trace_id: int) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM trace_invalidations WHERE trace_id=?"
                " ORDER BY invalidated_at, batch_no",
                (trace_id,),
            ).fetchall()

    def trace_history(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                "SELECT t.*,"
                " (SELECT COUNT(*) FROM trace_results r"
                "   WHERE r.trace_id=t.id) AS result_count,"
                " (SELECT COUNT(*) FROM trace_invalidations i"
                "   WHERE i.trace_id=t.id) AS invalidation_count"
                " FROM traces t ORDER BY t.id DESC"
            ).fetchall()

    # -------------------------------------------------- as-of inventory API

    def accept_inventory(
        self, *, site: str, as_of: datetime
    ) -> tuple[int, dict[str, Any]]:
        """Pin the current associations and batch versions for one query.

        The snapshot (sample associations plus referenced batch records) and
        the ``pending`` query row are written in a single ``BEGIN IMMEDIATE``
        transaction, so a concurrent intake or correction can never land
        between what was read and what was pinned.

        Raises:
            NotFoundError: no active dataset.
        """
        with self.transaction() as conn:
            snapshot = self._inventory_snapshot_in(conn)
            if snapshot is None:
                raise NotFoundError("no dataset")
            cur = conn.execute(
                "INSERT INTO inventory_queries(intake_id, status, site, as_of,"
                " accepted_at, assoc_json, pinned_json)"
                " VALUES (?, 'pending', ?, ?, ?, ?, ?)",
                (
                    snapshot["intake_id"],
                    site,
                    iso(as_of),
                    iso(utcnow()),
                    json.dumps(snapshot["associations"], sort_keys=True),
                    json.dumps(snapshot["pinned"], sort_keys=True),
                ),
            )
            return int(cur.lastrowid), snapshot

    @staticmethod
    def _assoc_batch_nos(associations: dict[str, list[str]]) -> set[str]:
        """Batch numbers actually referenced by the pinned associations."""
        return {batch_no for batch_nos in associations.values() for batch_no in batch_nos}

    @staticmethod
    def _inventory_snapshot_in(
        conn: sqlite3.Connection,
    ) -> dict[str, Any] | None:
        intake = conn.execute(
            "SELECT id FROM intakes WHERE status='active'"
        ).fetchone()
        if intake is None:
            return None
        intake_id = int(intake["id"])
        current = {
            row["batch_no"]: int(row["version"])
            for row in conn.execute(
                "SELECT batch_no, version FROM batches"
                " WHERE intake_id=? AND superseded=0",
                (intake_id,),
            ).fetchall()
        }
        associations: dict[str, list[str]] = {}
        for row in conn.execute(
            "SELECT sample_id, batch_no FROM sample_batches"
            " WHERE intake_id=? ORDER BY sample_id, batch_no",
            (intake_id,),
        ).fetchall():
            associations.setdefault(row["sample_id"], []).append(row["batch_no"])
        pinned = {
            batch_no: version
            for batch_no, version in current.items()
            if batch_no in Store._assoc_batch_nos(associations)
        }
        batches = [
            {
                "batch_no": row["batch_no"],
                "version": int(row["version"]),
                "origin": row["origin"],
                "destination": row["destination"],
                "departed_at": parse_iso(row["departed_at"]),
                "arrived_at": parse_iso(row["arrived_at"]),
            }
            for row in conn.execute(
                "SELECT batch_no, version, origin, destination, departed_at,"
                " arrived_at FROM batches WHERE intake_id=? AND superseded=0"
                " ORDER BY batch_no",
                (intake_id,),
            ).fetchall()
            if row["batch_no"] in pinned
        ]
        return {
            "intake_id": intake_id,
            "associations": associations,
            "pinned": pinned,
            "batches": batches,
        }

    def publish_inventory(
        self,
        query_id: int,
        outcome: dict[str, Any],
        *,
        pinned: dict[str, int],
        associations: dict[str, list[str]],
    ) -> None:
        """Publish result rows and the complete flag in one transaction.

        Refuses to publish when a pinned batch version or a pinned association
        set has changed since acceptance. Either the answer is based exactly on
        what was pinned, or nothing is published.
        """
        now = iso(utcnow())
        with self.transaction() as conn:
            query = conn.execute(
                "SELECT status, intake_id, assoc_json FROM inventory_queries"
                " WHERE id=?",
                (query_id,),
            ).fetchone()
            if query is None:
                raise NotFoundError("inventory query not found")
            if query["status"] != "pending":
                raise ConflictError(
                    f"inventory query {query_id} is {query['status']}, not pending"
                )
            intake_id = int(query["intake_id"])

            live = {
                row["batch_no"]: int(row["version"])
                for row in conn.execute(
                    "SELECT batch_no, version FROM batches"
                    " WHERE intake_id=? AND superseded=0",
                    (intake_id,),
                ).fetchall()
            }
            changed = sorted(
                batch_no
                for batch_no, version in pinned.items()
                if live.get(batch_no) != version
            )
            if changed:
                raise ConflictError(
                    "snapshot went stale during computation:"
                    f" {', '.join(changed)}"
                )

            relationships = {
                (row["sample_id"], row["batch_no"])
                for row in conn.execute(
                    "SELECT sample_id, batch_no FROM sample_batches"
                    " WHERE intake_id=?",
                    (intake_id,),
                ).fetchall()
            }
            pinned_relationships = {
                (sample_id, batch_no)
                for sample_id, batch_nos in associations.items()
                for batch_no in batch_nos
            }
            # Any genuinely new association changes the roster population, so
            # the pinned basis no longer matches; a sample that existed before
            # but now rides another batch is covered the same way.
            added = sorted(set(relationships) - pinned_relationships)
            if added:
                raise ConflictError(
                    "sample associations changed during computation:"
                    f" {', '.join(f'{s}->{b}' for s, b in added)}"
                )

            for rank, item in enumerate(outcome["in_stock_samples"]):
                conn.execute(
                    "INSERT INTO inventory_results(query_id, kind, rank,"
                    " sample_id, in_stock_since, in_stock_until, support_json)"
                    " VALUES (?, 'in_stock', ?, ?, ?, ?, ?)",
                    (
                        query_id,
                        rank,
                        item["sample_id"],
                        iso(item["in_stock_since"]),
                        iso(item["in_stock_until"])
                        if item["in_stock_until"] is not None
                        else None,
                        json.dumps(
                            [
                                {
                                    **leg,
                                    "departed_at": iso(leg["departed_at"]),
                                    "arrived_at": iso(leg["arrived_at"]),
                                }
                                for leg in item["supporting_batches"]
                            ]
                        ),
                    ),
                )
            for rank, item in enumerate(outcome["conflicting_samples"]):
                conn.execute(
                    "INSERT INTO inventory_results(query_id, kind, rank,"
                    " sample_id, conflicts_json)"
                    " VALUES (?, 'conflict', ?, ?, ?)",
                    (
                        query_id,
                        rank,
                        item["sample_id"],
                        json.dumps(
                            [
                                {
                                    **conflict,
                                    "batches": [
                                        {
                                            **leg,
                                            "departed_at": iso(leg["departed_at"]),
                                            "arrived_at": iso(leg["arrived_at"]),
                                        }
                                        for leg in conflict["batches"]
                                    ],
                                }
                                for conflict in item["conflicts"]
                            ]
                        ),
                    ),
                )
            conn.execute(
                "UPDATE inventory_queries SET status='complete',"
                " completed_at=?, error=NULL WHERE id=?",
                (now, query_id),
            )

    def fail_inventory(self, query_id: int, error: str) -> None:
        with self.transaction() as conn:
            conn.execute(
                "DELETE FROM inventory_results WHERE query_id=?", (query_id,)
            )
            conn.execute(
                "UPDATE inventory_queries SET status='failed', completed_at=?,"
                " error=? WHERE id=? AND status='pending'",
                (iso(utcnow()), error, query_id),
            )

    def read_inventory(self, query_id: int) -> dict[str, Any] | None:
        """Read a query, its rows and its invalidations in one transaction."""
        with self.connection() as conn:
            conn.execute("BEGIN")
            try:
                query = conn.execute(
                    "SELECT * FROM inventory_queries WHERE id=?", (query_id,)
                ).fetchone()
                if query is None:
                    return None
                results = conn.execute(
                    "SELECT * FROM inventory_results WHERE query_id=?"
                    " ORDER BY kind, rank",
                    (query_id,),
                ).fetchall()
                invalidations = conn.execute(
                    "SELECT * FROM inventory_invalidations WHERE query_id=?"
                    " ORDER BY seq",
                    (query_id,),
                ).fetchall()
                pinned = json.loads(query["pinned_json"])
                batches = [
                    {
                        "batch_no": row["batch_no"],
                        "version": int(row["version"]),
                        "origin": row["origin"],
                        "destination": row["destination"],
                        "departed_at": parse_iso(row["departed_at"]),
                        "arrived_at": parse_iso(row["arrived_at"]),
                    }
                    for row in conn.execute(
                        "SELECT batch_no, version, origin, destination,"
                        " departed_at, arrived_at FROM batches"
                        " WHERE intake_id=? AND superseded=0 ORDER BY batch_no",
                        (int(query["intake_id"]),),
                    ).fetchall()
                    if pinned.get(row["batch_no"]) == int(row["version"])
                ]
                return {
                    "query": query,
                    "results": results,
                    "invalidations": invalidations,
                    "pinned": pinned,
                    "associations": json.loads(query["assoc_json"]),
                    "batches": batches,
                }
            finally:
                conn.execute("COMMIT")

    def inventory(self, query_id: int) -> sqlite3.Row | None:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM inventory_queries WHERE id=?", (query_id,)
            ).fetchone()

    def inventory_invalidations(self, query_id: int) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                "SELECT * FROM inventory_invalidations WHERE query_id=?"
                " ORDER BY seq",
                (query_id,),
            ).fetchall()

    def inventory_history(self) -> list[sqlite3.Row]:
        with self.connection() as conn:
            return conn.execute(
                "SELECT q.*,"
                " (SELECT COUNT(*) FROM inventory_results r WHERE r.query_id=q.id"
                "   AND r.kind='in_stock') AS in_stock_count,"
                " (SELECT COUNT(*) FROM inventory_results r WHERE r.query_id=q.id"
                "   AND r.kind='conflict') AS conflict_count,"
                " (SELECT COUNT(*) FROM inventory_invalidations i"
                "   WHERE i.query_id=q.id) AS invalidation_count"
                " FROM inventory_queries q ORDER BY q.id DESC"
            ).fetchall()

    # ----------------------------------------------------------------- audit

    def log(self, kind: str, payload: Any) -> None:
        with self.transaction() as conn:
            conn.execute(
                "INSERT INTO request_log(kind, at, payload) VALUES (?, ?, ?)",
                (kind, iso(utcnow()), json.dumps(payload, sort_keys=True)),
            )
