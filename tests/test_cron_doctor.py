import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "scripts" / "cron-doctor.py"


def job(**overrides):
    value = {
        "id": "daily",
        "name": "Daily summary",
        "enabled": True,
        "state": "scheduled",
        "schedule": {"kind": "cron", "expr": "0 9 * * 1-5"},
        "next_run_at": "2099-01-01T09:00:00+00:00",
        "deliver": "telegram:-1001:7",
        "last_status": "ok",
    }
    value.update(overrides)
    return value


class CronDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        (self.home / "cron").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def stamp(self, name, text):
        (self.home / "cron" / name).write_text(text, encoding="utf-8")

    def run_doctor(self, jobs, *arguments, heartbeat=None, environment=None):
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
        now = time.time()
        if heartbeat is not False:
            self.stamp("ticker_heartbeat", heartbeat or f"{now} {os.getpid()}")
            self.stamp("ticker_last_success", str(now))
        env = os.environ | {"HERMES_HOME": str(self.home)} | (environment or {})
        if "HERMES_BIN" not in (environment or {}):
            arguments = ("--no-builtin", *arguments)
        return subprocess.run([sys.executable, str(SCRIPT), *arguments], env=env, text=True,
                              capture_output=True, encoding="utf-8")

    def payload(self, *args, **kwargs):
        result = self.run_doctor(*args, "--json", **kwargs)
        return result, json.loads(result.stdout)

    def executions_db(self, rows=(), incidents=()):
        connection = sqlite3.connect(self.home / "cron" / "executions.db")
        connection.execute("create table executions (id text, job_id text, status text, claimed_at text, "
                           "started_at text, finished_at text, error text, delivery_outcome text)")
        connection.execute("create table cron_incidents (id text, job_id text, error_sig text, state text, "
                           "error text, first_seen_at text, last_seen_at text)")
        connection.executemany("insert into executions values (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.executemany("insert into cron_incidents values (?, ?, ?, ?, ?, ?, ?)", incidents)
        connection.commit()
        connection.close()

    def test_healthy_job_is_zero_and_json(self):
        result, data = self.payload([job()])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["jobs"][0]["state"], "enabled")

    def test_heartbeat_with_pid_is_fresh(self):
        # Hermes 0.21.5 пишет `<epoch> <pid>`; раньше доктор читал это как «нет отметки».
        result, data = self.payload([job()])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(data["scheduler"]["heartbeat_pid"], os.getpid())
        self.assertLess(data["scheduler"]["heartbeat_age_seconds"], 60)

    def test_heartbeat_without_pid_still_accepted(self):
        result, data = self.payload([job()], heartbeat=str(time.time()))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIsNone(data["scheduler"]["heartbeat_pid"])

    @unittest.skipUnless(Path("/proc/self").is_dir(), "проверка писателя — только Linux /proc")
    def test_dead_heartbeat_writer_is_finding(self):
        result, data = self.payload([job()], heartbeat=f"{time.time()} 999999999")
        self.assertEqual(result.returncode, 1)
        self.assertIn("процесса 999999999", " ".join(data["findings"]))

    def test_missing_heartbeat_is_finding(self):
        result, data = self.payload([job()], heartbeat=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("нет ticker_heartbeat", " ".join(data["findings"]))

    def test_half_paused_record_is_finding(self):
        result, data = self.payload([job(state="paused")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(data["jobs"][0]["state"], "half-paused")
        self.assertIn("Hermes её не запускает", " ".join(data["findings"]))

    def test_paused_at_alone_is_a_pause_marker(self):
        _, data = self.payload([job(paused_at="2026-09-01T00:00:00+00:00")])
        self.assertEqual(data["jobs"][0]["state"], "half-paused")

    def test_disabled_without_pause_marker_is_not_a_finding(self):
        result, data = self.payload([job(enabled=False)])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(data["jobs"][0]["state"], "disabled")

    def test_failed_statuses_checked_without_builtin(self):
        for status in ("error", "blocked_config", "interrupted"):
            result, data = self.payload([job(last_status=status, last_error="boom")])
            self.assertEqual(result.returncode, 1, status)
            self.assertIn(status, " ".join(data["findings"]))

    def test_delivery_failed_status_reports_delivery_error(self):
        result, data = self.payload([job(last_status="delivery_failed", last_delivery_error="chat not found")])
        self.assertEqual(result.returncode, 1)
        self.assertIn("не доставлен", " ".join(data["findings"]))

    def test_held_is_note_not_finding(self):
        result, data = self.payload([job(last_status="held")])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("held", " ".join(data["notes"]))

    def test_overdue_uses_fifteen_minute_grace(self):
        recent = datetime_iso(-10 * 60)
        late = datetime_iso(-20 * 60)
        self.assertEqual(self.run_doctor([job(next_run_at=recent)]).returncode, 0)
        result, data = self.payload([job(next_run_at=late)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("просрочен", " ".join(data["findings"]))

    def test_failure_streak_is_finding(self):
        result, data = self.payload([job(failure_streak=3)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("3 неудачных", " ".join(data["findings"]))

    def test_thread_note_only_for_chats_with_threads(self):
        direct = job(id="dm", deliver="telegram:42")
        threaded = job(id="topic", deliver="telegram:-1001:7")
        lobby = job(id="lobby", deliver="telegram:-1001")
        _, data = self.payload([direct, threaded, lobby])
        notes = " ".join(data["notes"])
        self.assertIn("lobby", notes)
        self.assertNotIn("dm «", notes)

    def test_pinned_model_is_note(self):
        _, data = self.payload([job(model="model-x", provider="provider-y")])
        self.assertIn("закреплена модель (model-x · provider-y)", " ".join(data["notes"]))
        _, data = self.payload([job(model="model-x", no_agent=True, script="ping.py")])
        self.assertNotIn("закреплена", " ".join(data["notes"]))

    def test_open_incident_and_failed_delivery_from_history(self):
        self.executions_db(
            rows=[("e1", "daily", "completed", "2026-09-30T00:00:00+00:00", None, None, None, "failed")],
            incidents=[("inc1", "daily", "sig", "alerted", "provider 500", "x", "y"),
                       ("inc2", "daily", "sig2", "closed", "old", "x", "y")])
        result, data = self.payload([job()])
        findings = " ".join(data["findings"])
        self.assertEqual(result.returncode, 1)
        self.assertIn("hermes cron incidents ack inc1", findings)
        self.assertNotIn("inc2", findings)
        self.assertIn("delivery_outcome=failed", findings)

    def test_job_detail_shows_runs_and_delivery(self):
        self.executions_db(rows=[("e1", "daily", "completed", "2026-09-30T00:00:00+00:00", None, None, "", "delivered")])
        result = self.run_doctor([job()], "--job", "daily")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("доставка: delivered", result.stdout)

    def test_builtin_doctor_output_is_included(self):
        fake = self.home / "fake-hermes.py"
        fake.write_text("import sys\nprint('Cron doctor found 1 issue(s)')\nsys.exit(1)\n", encoding="utf-8")
        launcher = self.home / ("fake-hermes.cmd" if os.name == "nt" else "fake-hermes")
        if os.name == "nt":
            launcher.write_text(f'@"{sys.executable}" "{fake}" %*\n', encoding="utf-8")
        else:
            launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)
        # Без встроенного доктора базовые проверки делает наш; со встроенным — не дублирует.
        result, data = self.payload([job(last_status="error")], environment={"HERMES_BIN": str(launcher)})
        self.assertTrue(data["builtin"]["ran"], data["builtin"])
        self.assertEqual(result.returncode, 1)
        findings = " ".join(data["findings"])
        self.assertIn("встроенный", findings)
        self.assertNotIn("последний запуск — error", findings)

    def test_unknown_job_is_exit_three(self):
        self.assertEqual(self.run_doctor([job()], "--job", "nope").returncode, 3)

    def test_invalid_arguments_are_exit_three(self):
        self.assertEqual(self.run_doctor([job()], "--runs", "0").returncode, 3)

    def test_missing_runtime_is_exit_two(self):
        env = os.environ | {"HERMES_HOME": str(self.home)}
        result = subprocess.run([sys.executable, str(SCRIPT), "--json", "--no-builtin"], env=env,
                                text=True, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2)
        self.assertIn("jobs.json", result.stdout)


def datetime_iso(offset_seconds):
    from datetime import datetime, timedelta, timezone
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


if __name__ == "__main__":
    unittest.main()
