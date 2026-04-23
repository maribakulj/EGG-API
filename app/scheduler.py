"""Import scheduler (Sprint 27).

Before Sprint 27 every import source ran only when the operator clicked
*Run now*. Libraries and archives expect a catalogue to stay fresh
without somebody opening the admin UI every morning — so this module
adds a lightweight cron-like loop that picks sources whose
``next_run_at`` has elapsed and triggers them through the same
``run_import`` dispatcher the REST and UI endpoints use.

Design notes:

* **No third-party scheduler** (no APScheduler, Celery, Redis queue).
  SQLite is the source of truth; a single background thread polls
  every ``tick_seconds`` (default 60) and races nobody — imports are
  always queued sequentially to keep SIGB and DAMS peers happy.
* **Cadence vocabulary is deliberately small** (``hourly``, ``6h``,
  ``daily``, ``weekly``) so the admin UI can render a dropdown rather
  than a cron expression field. Operators are archivists, not DevOps.
* **The loop never blocks startup**: ``Scheduler.start()`` spawns a
  daemon thread and returns immediately. ``Scheduler.stop()`` is
  idempotent and waits for the in-flight tick to finish.

The scheduler is *opt-in per source*: a source with ``schedule=None``
or ``next_run_at=None`` is ignored. The admin UI still offers *Run now*
for ad-hoc runs and testing.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timedelta, timezone
from typing import TYPE_CHECKING, Any

from app.importers import run_import

if TYPE_CHECKING:
    from app.storage.sqlite_store import ImportSource, SQLiteStore

logger = logging.getLogger("egg.scheduler")


# Human-friendly cadence string → timedelta. The admin UI only lets
# operators pick from these four values; anything else on a row is
# treated as manual (next_run_at is wiped on load).
SCHEDULE_DELTAS: dict[str, timedelta] = {
    "hourly": timedelta(hours=1),
    "6h": timedelta(hours=6),
    "daily": timedelta(hours=24),
    "weekly": timedelta(days=7),
}


def is_valid_schedule(value: str | None) -> bool:
    return value in SCHEDULE_DELTAS


def compute_next_run_at(schedule: str | None, *, now: datetime | None = None) -> str | None:
    """Return the ISO timestamp for the next run given a cadence.

    Returns ``None`` when ``schedule`` is empty / unknown, which signals
    the scheduler to leave the source alone.
    """

    if schedule not in SCHEDULE_DELTAS:
        return None
    base = now or datetime.now(timezone.utc)
    return (base + SCHEDULE_DELTAS[schedule]).isoformat()


class Scheduler:
    """Background thread that runs due import sources."""

    def __init__(
        self,
        *,
        store: SQLiteStore,
        bulk_index: Any,
        tick_seconds: float = 60.0,
    ) -> None:
        self._store = store
        self._bulk_index = bulk_index
        self._tick_seconds = max(1.0, float(tick_seconds))
        self._stop_event = threading.Event()
        self._thread: threading.Thread | None = None

    # -- lifecycle -----------------------------------------------------------

    def start(self) -> None:
        """Start the polling thread if it's not already running."""

        if self._thread is not None and self._thread.is_alive():
            return
        self._stop_event.clear()
        self._thread = threading.Thread(
            target=self._loop,
            name="egg-import-scheduler",
            daemon=True,
        )
        self._thread.start()
        logger.info("scheduler_started", extra={"tick_seconds": self._tick_seconds})

    def stop(self, *, timeout: float = 5.0) -> None:
        """Signal the polling thread to stop and wait for it to exit."""

        self._stop_event.set()
        thread = self._thread
        if thread is not None and thread.is_alive():
            thread.join(timeout=timeout)
        self._thread = None
        logger.info("scheduler_stopped")

    # -- polling -------------------------------------------------------------

    def _loop(self) -> None:
        while not self._stop_event.is_set():
            try:
                self.run_pending()
            except Exception:  # pragma: no cover - defensive
                logger.exception("scheduler_tick_failed")
            # ``wait`` returns True when the event is set (i.e. stop was
            # called) — break out immediately in that case.
            if self._stop_event.wait(self._tick_seconds):
                break

    def run_pending(self, *, now: datetime | None = None) -> list[int]:
        """Pick every due source and run it once. Returns the ids touched.

        Kept synchronous and public so tests can drive the scheduler
        without the background thread — a single ``run_pending()`` call
        is enough to assert the cadence + dispatcher wiring.
        """

        current = now or datetime.now(timezone.utc)
        current_iso = current.isoformat()
        due = self._store.list_due_import_sources(now=current_iso)
        touched: list[int] = []
        for source in due:
            touched.append(source.id)
            self._run_one(source, now=current)
        return touched

    def _run_one(self, source: ImportSource, *, now: datetime) -> None:
        run_id = self._store.start_import_run(source.id)
        try:
            result = run_import(source, bulk_index=self._bulk_index)
        except Exception as exc:
            self._store.finish_import_run(
                run_id,
                status="failed",
                records_ingested=0,
                records_failed=0,
                error_message=str(exc),
            )
            logger.exception(
                "scheduler_import_failed",
                extra={"source_id": source.id, "label": source.label},
            )
            self._reschedule(source, now=now)
            return

        status = "failed" if result.error else "succeeded"
        self._store.finish_import_run(
            run_id,
            status=status,
            records_ingested=result.ingested,
            records_failed=result.failed,
            error_message=result.error,
        )
        self._reschedule(source, now=now)
        logger.info(
            "scheduler_import_done",
            extra={
                "source_id": source.id,
                "label": source.label,
                "status": status,
                "records_ingested": result.ingested,
                "records_failed": result.failed,
            },
        )

    def _reschedule(self, source: ImportSource, *, now: datetime) -> None:
        if not is_valid_schedule(source.schedule):
            # The operator cleared the cadence between two ticks — wipe
            # next_run_at so the loop stops picking this source.
            self._store.set_import_source_schedule(source.id, schedule=None, next_run_at=None)
            return
        next_iso = compute_next_run_at(source.schedule, now=now)
        self._store.set_import_source_schedule(
            source.id, schedule=source.schedule, next_run_at=next_iso
        )
