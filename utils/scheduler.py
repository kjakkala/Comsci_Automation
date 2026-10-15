import json
import sqlite3
import threading
from contextlib import contextmanager
from datetime import datetime, timedelta
from pathlib import Path
from typing import Callable, Iterator


RECURRENCES = {"once", "weekly", "monthly"}


def next_occurrence(current: datetime, recurrence: str, anchor_day: int | None = None) -> datetime | None:
    if recurrence == "once":
        return None
    if recurrence == "weekly":
        return current + timedelta(days=7)
    if recurrence != "monthly":
        raise ValueError(f"Unsupported recurrence: {recurrence}")

    year = current.year + (1 if current.month == 12 else 0)
    month = 1 if current.month == 12 else current.month + 1
    day = min(anchor_day or current.day, _days_in_month(year, month))
    return current.replace(year=year, month=month, day=day)


def next_future_occurrence(
    due: datetime,
    recurrence: str,
    anchor_day: int | None,
    now: datetime,
) -> datetime | None:
    candidate = next_occurrence(due, recurrence, anchor_day)
    while candidate is not None and candidate <= now:
        candidate = next_occurrence(candidate, recurrence, anchor_day)
    return candidate


def _days_in_month(year: int, month: int) -> int:
    if month == 12:
        next_month = datetime(year + 1, 1, 1)
    else:
        next_month = datetime(year, month + 1, 1)
    return (next_month - timedelta(days=1)).day


class ScheduleStore:
    def __init__(self, database_path: Path, log_directory: Path):
        self.database_path = Path(database_path)
        self.log_directory = Path(log_directory)
        self.database_path.parent.mkdir(parents=True, exist_ok=True)
        self.log_directory.mkdir(parents=True, exist_ok=True)
        self._lock = threading.RLock()
        self._initialize()

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.database_path, timeout=30)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys = ON")
        try:
            yield connection
            connection.commit()
        except Exception:
            connection.rollback()
            raise
        finally:
            connection.close()

    def _initialize(self) -> None:
        with self._lock, self._connect() as connection:
            connection.executescript(
                """
                CREATE TABLE IF NOT EXISTS schedules (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    name TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    sections_json TEXT NOT NULL,
                    recurrence TEXT NOT NULL,
                    next_run TEXT,
                    anchor_day INTEGER NOT NULL,
                    timezone TEXT NOT NULL,
                    enabled INTEGER NOT NULL DEFAULT 1,
                    created_at TEXT NOT NULL
                );
                CREATE TABLE IF NOT EXISTS run_history (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    schedule_id INTEGER REFERENCES schedules(id) ON DELETE SET NULL,
                    schedule_name TEXT NOT NULL,
                    environment TEXT NOT NULL,
                    sections_json TEXT NOT NULL,
                    started_at TEXT NOT NULL,
                    finished_at TEXT,
                    outcome TEXT NOT NULL,
                    log_file TEXT NOT NULL
                );
                CREATE INDEX IF NOT EXISTS idx_schedules_due
                    ON schedules(enabled, next_run);
                CREATE INDEX IF NOT EXISTS idx_run_history_started
                    ON run_history(started_at DESC);
                """
            )

    def create_schedule(
        self,
        name: str,
        environment: str,
        sections: list[str],
        recurrence: str,
        first_run: datetime,
        timezone: str,
    ) -> int:
        if recurrence not in RECURRENCES:
            raise ValueError("Choose a one-time, weekly, or monthly schedule.")
        if not name.strip() or not environment or not sections:
            raise ValueError("Name, environment, and at least one section are required.")
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO schedules
                   (name, environment, sections_json, recurrence, next_run, anchor_day, timezone, created_at)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
                (
                    name.strip(),
                    environment,
                    json.dumps(sections),
                    recurrence,
                    first_run.isoformat(timespec="seconds"),
                    first_run.day,
                    timezone,
                    datetime.now().isoformat(timespec="seconds"),
                ),
            )
            return int(cursor.lastrowid)

    def list_schedules(self) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                """SELECT schedules.*,
                          EXISTS(SELECT 1 FROM run_history
                                 WHERE run_history.schedule_id = schedules.id
                                   AND run_history.outcome = 'running') AS is_running
                   FROM schedules ORDER BY next_run, id"""
            ).fetchall()
        return [self._schedule_dict(row) for row in rows]

    def get_due_schedules(self, now: datetime) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM schedules WHERE enabled = 1 AND next_run IS NOT NULL AND next_run <= ? ORDER BY next_run, id",
                (now.isoformat(timespec="seconds"),),
            ).fetchall()
        return [self._schedule_dict(row) for row in rows]

    def mark_started(self, schedule: dict, started_at: datetime) -> int:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                """INSERT INTO run_history
                   (schedule_id, schedule_name, environment, sections_json, started_at, outcome, log_file)
                   VALUES (?, ?, ?, ?, ?, 'running', ?)""",
                (
                    schedule["id"],
                    schedule["name"],
                    schedule["environment"],
                    json.dumps(schedule["sections"]),
                    started_at.isoformat(timespec="seconds"),
                    f"run_{started_at.strftime('%Y%m%d_%H%M%S')}_{schedule['id']}.log",
                ),
            )
            run_id = int(cursor.lastrowid)
            connection.execute(
                "UPDATE run_history SET log_file = ? WHERE id = ?",
                (f"run_{run_id}_{started_at.strftime('%Y%m%d_%H%M%S')}.log", run_id),
            )
        log_path = self.get_log_path(run_id)
        if log_path is None:
            raise RuntimeError("The scheduled run log record was not persisted.")
        log_path.write_text(
            "Scheduled run started\n"
            f"Schedule: {schedule['name']}\n"
            f"Start: {started_at.isoformat(timespec='seconds')} {schedule['timezone']}\n"
            f"Environment: {schedule['environment']}\n"
            f"Sections: {', '.join(schedule['sections'])}\n\n",
            encoding="utf-8",
        )
        return run_id

    def cancel_unstarted_run(self, run_id: int) -> None:
        log_path = self.get_log_path(run_id)
        with self._lock, self._connect() as connection:
            connection.execute("DELETE FROM run_history WHERE id = ? AND outcome = 'running'", (run_id,))
        if log_path:
            log_path.unlink(missing_ok=True)

    def advance_after_start(self, schedule: dict, started_at: datetime) -> None:
        if schedule["recurrence"] == "once":
            enabled, next_run = 0, None
        else:
            enabled = 1
            next_run = next_future_occurrence(
                datetime.fromisoformat(schedule["next_run"]),
                schedule["recurrence"],
                schedule["anchor_day"],
                started_at,
            )
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE schedules SET enabled = ?, next_run = ? WHERE id = ?",
                (enabled, next_run.isoformat(timespec="seconds") if next_run else None, schedule["id"]),
            )

    def append_log(self, run_id: int, content: str) -> None:
        log_path = self.get_log_path(run_id)
        if log_path is None:
            return
        with log_path.open("a", encoding="utf-8") as log_file:
            log_file.write(content)

    def finish_run(self, run_id: int, finished_at: datetime, outcome: str) -> None:
        with self._lock, self._connect() as connection:
            connection.execute(
                "UPDATE run_history SET finished_at = ?, outcome = ? WHERE id = ?",
                (finished_at.isoformat(timespec="seconds"), outcome, run_id),
            )

    def recover_interrupted_runs(self, recovered_at: datetime) -> int:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT id FROM run_history WHERE outcome = 'running'"
            ).fetchall()
            connection.execute(
                "UPDATE run_history SET finished_at = ?, outcome = 'interrupted' WHERE outcome = 'running'",
                (recovered_at.isoformat(timespec="seconds"),),
            )
        for row in rows:
            self.append_log(
                row["id"],
                f"\nFinished: {recovered_at.isoformat(timespec='seconds')} local time\n"
                "Outcome: interrupted by dashboard restart.\n",
            )
        return len(rows)

    def list_history(self, limit: int = 50) -> list[dict]:
        with self._lock, self._connect() as connection:
            rows = connection.execute(
                "SELECT * FROM run_history ORDER BY started_at DESC, id DESC LIMIT ?", (limit,)
            ).fetchall()
        history = []
        for row in rows:
            item = dict(row)
            item["sections"] = json.loads(item.pop("sections_json"))
            item["log_file"] = Path(row["log_file"]).name
            history.append(item)
        return history

    def set_enabled(self, schedule_id: int, enabled: bool) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute(
                "UPDATE schedules SET enabled = ? WHERE id = ? AND next_run IS NOT NULL",
                (int(enabled), schedule_id),
            )
            return cursor.rowcount > 0

    def delete_schedule(self, schedule_id: int) -> bool:
        with self._lock, self._connect() as connection:
            cursor = connection.execute("DELETE FROM schedules WHERE id = ?", (schedule_id,))
            return cursor.rowcount > 0

    def get_log_path(self, run_id: int) -> Path | None:
        with self._lock, self._connect() as connection:
            row = connection.execute("SELECT log_file FROM run_history WHERE id = ?", (run_id,)).fetchone()
        if not row:
            return None
        return self.log_directory / Path(row["log_file"]).name

    @staticmethod
    def _schedule_dict(row: sqlite3.Row) -> dict:
        schedule = dict(row)
        schedule["sections"] = json.loads(schedule.pop("sections_json"))
        schedule["enabled"] = bool(schedule["enabled"])
        is_running = bool(schedule.pop("is_running", False))
        if is_running:
            schedule["status"] = "running"
        elif schedule["next_run"] is None:
            schedule["status"] = "completed"
        else:
            schedule["status"] = "active" if schedule["enabled"] else "paused"
        return schedule


class SchedulerWorker:
    def __init__(
        self,
        store: ScheduleStore,
        start_due_run: Callable[[dict, datetime], bool],
        interval_seconds: float = 5,
    ):
        self.store = store
        self.start_due_run = start_due_run
        self.interval_seconds = interval_seconds
        self._stop = threading.Event()
        self._scan_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="dashboard-scheduler", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=max(1, self.interval_seconds * 2))

    def run_once(self, now: datetime | None = None) -> int:
        if not self._scan_lock.acquire(blocking=False):
            return 0
        try:
            current_time = now or datetime.now()
            started = 0
            for schedule in self.store.get_due_schedules(current_time):
                try:
                    started += bool(self.start_due_run(schedule, current_time))
                except Exception:
                    continue
            return started
        finally:
            self._scan_lock.release()

    def _run(self) -> None:
        while not self._stop.is_set():
            self.run_once()
            self._stop.wait(self.interval_seconds)