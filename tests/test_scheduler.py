from datetime import datetime

import dashboard_server as dashboard
from utils.scheduler import ScheduleStore, SchedulerWorker, next_future_occurrence, next_occurrence


def test_monthly_recurrence_clamps_to_month_end_and_restores_anchor_day():
    january = datetime(2026, 1, 31, 9, 15)
    february = next_occurrence(january, "monthly", anchor_day=31)
    march = next_occurrence(february, "monthly", anchor_day=31)

    assert february == datetime(2026, 2, 28, 9, 15)
    assert march == datetime(2026, 3, 31, 9, 15)


def test_weekly_recurrence_and_missed_run_advance():
    due = datetime(2026, 10, 1, 8, 30)
    assert next_occurrence(due, "weekly") == datetime(2026, 10, 8, 8, 30)
    assert next_future_occurrence(due, "weekly", due.day, datetime(2026, 10, 20)) == datetime(2026, 10, 22, 8, 30)


def test_schedule_and_run_history_persist_in_sqlite(tmp_path):
    database = tmp_path / "scheduler.sqlite3"
    logs = tmp_path / "logs"
    store = ScheduleStore(database, logs)
    scheduled_for = datetime(2026, 10, 20, 10, 0)
    schedule_id = store.create_schedule("Nightly", "QA", ["Authentication"], "weekly", scheduled_for, "UTC")
    schedule = store.list_schedules()[0]
    run_id = store.mark_started(schedule, datetime(2026, 10, 20, 10, 0))
    store.append_log(run_id, "pytest output\n")
    store.finish_run(run_id, datetime(2026, 10, 20, 10, 2), "passed")

    reopened = ScheduleStore(database, logs)
    assert reopened.list_schedules()[0]["id"] == schedule_id
    history = reopened.list_history()
    assert history[0]["outcome"] == "passed"
    assert reopened.get_log_path(run_id).read_text(encoding="utf-8").endswith("pytest output\n")


def test_missed_one_time_run_is_started_after_worker_restart(tmp_path):
    store = ScheduleStore(tmp_path / "scheduler.sqlite3", tmp_path / "logs")
    missed_time = datetime(2026, 10, 1, 8, 0)
    schedule_id = store.create_schedule("Missed", "QA", ["Header"], "once", missed_time, "UTC")
    restarted_store = ScheduleStore(tmp_path / "scheduler.sqlite3", tmp_path / "logs")
    started = []

    def start_due(schedule, now):
        started.append((schedule["id"], now))
        run_id = restarted_store.mark_started(schedule, now)
        restarted_store.finish_run(run_id, now, "passed")
        restarted_store.advance_after_start(schedule, now)
        return True

    worker = SchedulerWorker(restarted_store, start_due)
    assert worker.run_once(datetime(2026, 10, 4, 8, 0)) == 1
    assert started[0][0] == schedule_id
    assert restarted_store.list_schedules()[0]["status"] == "completed"
    assert restarted_store.list_history()[0]["outcome"] == "passed"


def test_one_time_schedule_reports_running_until_the_run_finishes(tmp_path):
    store = ScheduleStore(tmp_path / "scheduler.sqlite3", tmp_path / "logs")
    store.create_schedule("Once", "QA", ["Header"], "once", datetime.now(), "UTC")
    schedule = store.list_schedules()[0]
    run_id = store.mark_started(schedule, datetime.now())
    store.advance_after_start(schedule, datetime.now())

    assert store.list_schedules()[0]["status"] == "running"
    store.finish_run(run_id, datetime.now(), "passed")
    assert store.list_schedules()[0]["status"] == "completed"


def test_restart_marks_abandoned_run_interrupted(tmp_path):
    database = tmp_path / "scheduler.sqlite3"
    logs = tmp_path / "logs"
    store = ScheduleStore(database, logs)
    store.create_schedule("Interrupted", "QA", ["Header"], "weekly", datetime.now(), "UTC")
    run_id = store.mark_started(store.list_schedules()[0], datetime.now())

    recovered = ScheduleStore(database, logs)
    assert recovered.recover_interrupted_runs(datetime(2026, 10, 8, 10, 0)) == 1
    assert recovered.list_history()[0]["outcome"] == "interrupted"
    assert "Outcome: interrupted" in recovered.get_log_path(run_id).read_text(encoding="utf-8")


def test_due_run_stays_due_when_manual_run_owns_runner(tmp_path, monkeypatch):
    store = ScheduleStore(tmp_path / "scheduler.sqlite3", tmp_path / "logs")
    schedule_id = store.create_schedule("Busy", "QA", ["Header"], "once", datetime.now(), "UTC")
    monkeypatch.setattr(dashboard, "_scheduler_store", store)
    monkeypatch.setattr(dashboard, "_scheduler_environments", lambda: {"QA": "https://qa.example"})
    monkeypatch.setitem(dashboard._live_run_state, "running", True)

    schedule = store.list_schedules()[0]
    assert dashboard._launch_scheduled_run(schedule, datetime.now()) is False
    assert store.list_schedules()[0]["id"] == schedule_id
    assert store.list_schedules()[0]["status"] == "active"
    assert store.list_history() == []


def test_due_schedules_do_not_overlap_each_other(tmp_path, monkeypatch):
    store = ScheduleStore(tmp_path / "scheduler.sqlite3", tmp_path / "logs")
    due_at = datetime(2026, 10, 8, 9, 0)
    store.create_schedule("First", "QA", ["Authentication"], "once", due_at, "UTC")
    store.create_schedule("Second", "QA", ["Header"], "once", due_at, "UTC")
    monkeypatch.setattr(dashboard, "_scheduler_store", store)
    monkeypatch.setattr(dashboard, "_scheduler_environments", lambda: {"QA": "https://qa.example"})
    active = {"value": False}

    def start_shared_runner(*args, **kwargs):
        if active["value"]:
            return False
        active["value"] = True
        return True

    monkeypatch.setattr(dashboard, "_start_section_run", start_shared_runner)
    worker = SchedulerWorker(store, dashboard._launch_scheduled_run)

    assert worker.run_once(due_at) == 1
    assert len(store.list_history()) == 1
    assert sum(schedule["status"] == "running" for schedule in store.list_schedules()) == 1
    assert sum(schedule["status"] == "active" for schedule in store.list_schedules()) == 1


def test_scheduled_run_uses_shared_runner_and_redacts_credentials(tmp_path, monkeypatch):
    store = ScheduleStore(tmp_path / "scheduler.sqlite3", tmp_path / "logs")
    schedule_id = store.create_schedule(
        "Shared runner", "QA", ["Authentication", "Header"], "once", datetime.now(), "UTC"
    )
    monkeypatch.setattr(dashboard, "_scheduler_store", store)
    monkeypatch.setattr(dashboard, "_scheduler_environments", lambda: {"QA": "https://qa.example"})
    monkeypatch.setenv("LOGIN_PASSWORD", "secret-password")
    received = {}

    def start_shared_runner(args, section, mode, **callbacks):
        received.update(args=args, section=section, mode=mode)
        callbacks["on_output"]("Login used secret-password\n")
        callbacks["on_complete"](
            {"returncode": 0, "finished_at": datetime.now().isoformat(), "testrail": {"published": 2}}
        )
        return True

    monkeypatch.setattr(dashboard, "_start_section_run", start_shared_runner)
    assert dashboard._launch_scheduled_run(store.list_schedules()[0], datetime.now()) is True
    log_text = store.get_log_path(1).read_text(encoding="utf-8")
    assert received == {
        "args": ["tests/login", "tests/header"],
        "section": "All sections",
        "mode": "scheduled",
    }
    assert "Environment: QA" in log_text
    assert "Sections: Authentication, Header" in log_text
    assert "secret-password" not in log_text
    assert "[REDACTED]" in log_text
    assert "Outcome: passed" in log_text


def test_scheduler_page_controls_and_log_download(tmp_path, monkeypatch):
    store = ScheduleStore(tmp_path / "scheduler.sqlite3", tmp_path / "logs")
    monkeypatch.setattr(dashboard, "_scheduler_store", store)
    monkeypatch.setattr(dashboard, "_scheduler_environments", lambda: {"QA": "https://qa.example"})
    client = dashboard.app.test_client()
    response = client.post(
        "/scheduler/create",
        data={
            "name": "Night run",
            "environment": "QA",
            "recurrence": "monthly",
            "first_run": "2026-10-31T23:00",
            "sections": ["__all__"],
        },
    )
    assert response.status_code == 302
    schedule = store.list_schedules()[0]
    assert schedule["sections"] == ["Authentication", "Header"]
    page = client.get("/scheduler")
    assert b"server's local time zone" in page.data
    assert b"Night run" in page.data

    assert client.post(f"/scheduler/{schedule['id']}/toggle", data={"enabled": "0"}).status_code == 302
    assert store.list_schedules()[0]["status"] == "paused"
    assert client.post(f"/scheduler/{schedule['id']}/toggle", data={"enabled": "1"}).status_code == 302
    assert store.list_schedules()[0]["status"] == "active"

    run_id = store.mark_started(schedule, datetime(2026, 10, 31, 23, 0))
    store.append_log(run_id, "sample output\n")
    store.finish_run(run_id, datetime(2026, 10, 31, 23, 2), "passed")
    download = client.get(f"/scheduler/logs/{run_id}")
    assert download.status_code == 200
    assert download.headers["Content-Disposition"].startswith("attachment;")
    assert b"sample output" in download.data

    assert client.post(f"/scheduler/{schedule['id']}/delete").status_code == 302
    assert store.list_schedules() == []
    assert store.list_history()[0]["id"] == run_id