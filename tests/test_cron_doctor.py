import hashlib
import json
import os
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone
from pathlib import Path


SCRIPT = Path(__file__).parents[1] / "skills" / "hermes-cron" / "scripts" / "cron-doctor.py"


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


def datetime_iso(offset_seconds):
    return (datetime.now(timezone.utc) + timedelta(seconds=offset_seconds)).isoformat()


class CronDoctorTests(unittest.TestCase):
    def setUp(self):
        self.temporary = tempfile.TemporaryDirectory()
        self.home = Path(self.temporary.name)
        (self.home / "cron").mkdir()

    def tearDown(self):
        self.temporary.cleanup()

    def stamp(self, name, text):
        (self.home / "cron" / name).write_text(text, encoding="utf-8")

    def write_jobs(self, jobs):
        (self.home / "cron" / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")

    def run_doctor(self, jobs, *arguments, heartbeat=None, environment=None, write=True):
        if write:
            self.write_jobs(jobs)
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
        self.assertNotIn("Traceback", result.stderr)
        return result, json.loads(result.stdout)

    def executions_db(self, rows=(), incidents=(), incident_columns=None):
        connection = sqlite3.connect(self.home / "cron" / "executions.db")
        connection.execute("create table executions (id text, job_id text, status text, claimed_at text, "
                           "started_at text, finished_at text, error text, delivery_outcome text)")
        if incident_columns is None:
            connection.execute("create table cron_incidents (id text, job_id text, error_sig text, state text, "
                               "error text, first_seen_at text, last_seen_at text)")
            connection.executemany("insert into cron_incidents values (?, ?, ?, ?, ?, ?, ?)", incidents)
        elif incident_columns:
            connection.execute(f"create table cron_incidents ({incident_columns})")
        connection.executemany("insert into executions values (?, ?, ?, ?, ?, ?, ?, ?)", rows)
        connection.commit()
        connection.close()

    # --- basics ---------------------------------------------------------------------------------

    def test_healthy_job_is_zero_and_json(self):
        result, data = self.payload([job()])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(data["ok"])
        self.assertEqual(data["jobs"][0]["state"], "enabled")

    def test_missing_runtime_is_exit_two(self):
        env = os.environ | {"HERMES_HOME": str(self.home)}
        result = subprocess.run([sys.executable, str(SCRIPT), "--json", "--no-builtin"], env=env,
                                text=True, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2)
        self.assertIn("jobs.json", result.stdout)

    def test_invalid_utf8_jobs_file_is_exit_two_not_traceback(self):
        (self.home / "cron" / "jobs.json").write_bytes(b'{"jobs": ["\xff\xfe"]}')
        result, data = self.payload([], write=False)
        self.assertEqual(result.returncode, 2)
        self.assertIn("cannot read", data["error"])

    def test_unknown_job_is_exit_three(self):
        self.assertEqual(self.run_doctor([job()], "--job", "nope").returncode, 3)

    def test_invalid_arguments_are_exit_three(self):
        self.assertEqual(self.run_doctor([job()], "--runs", "0").returncode, 3)

    def test_invalid_arguments_with_json_print_json(self):
        result = self.run_doctor([job()], "--json", "--runs", "0")
        self.assertEqual(result.returncode, 3)
        self.assertFalse(json.loads(result.stdout)["ok"])

    def test_no_builtin_pass_changes_nothing_on_disk(self):
        self.executions_db(rows=[("e1", "daily", "completed", "2026-09-30T00:00:00+00:00", None, None, "", "delivered")])
        self.write_jobs([job()])
        files = sorted((self.home / "cron").iterdir())
        before = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in files}
        self.run_doctor([], heartbeat=False, write=False)
        self.run_doctor([], "--job", "daily", heartbeat=False, write=False)
        after = {f.name: hashlib.sha256(f.read_bytes()).hexdigest() for f in sorted((self.home / "cron").iterdir())}
        self.assertEqual(before, after)

    # --- ticker stamps --------------------------------------------------------------------------

    def test_heartbeat_with_pid_is_fresh(self):
        # Hermes 0.21.5 writes `<epoch> <pid>`; the old parser read it as "no stamp".
        result, data = self.payload([job()])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(data["scheduler"]["heartbeat_pid"], os.getpid())
        self.assertLess(data["scheduler"]["heartbeat_age_seconds"], 60)

    def test_heartbeat_without_pid_still_accepted(self):
        result, data = self.payload([job()], heartbeat=str(time.time()))
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIsNone(data["scheduler"]["heartbeat_pid"])

    def test_missing_heartbeat_is_finding(self):
        result, data = self.payload([job()], heartbeat=False)
        self.assertEqual(result.returncode, 1)
        self.assertIn("no ticker_heartbeat", " ".join(data["findings"]))

    def test_corrupt_heartbeat_values_are_findings(self):
        for value in ("nan 1", "inf 1", "-inf 1", "0", "garbage"):
            result, data = self.payload([job()], heartbeat=value)
            self.assertEqual(result.returncode, 1, value)
            self.assertIn("ticker_heartbeat", " ".join(data["findings"]), value)

    def test_future_heartbeat_is_not_fresh(self):
        result, data = self.payload([job()], heartbeat=f"{time.time() + 86400} 1")
        self.assertEqual(result.returncode, 1)
        self.assertIn("in the future", " ".join(data["findings"]))

    # --- job records ----------------------------------------------------------------------------

    def test_half_paused_record_is_finding(self):
        result, data = self.payload([job(state="paused")])
        self.assertEqual(result.returncode, 1)
        self.assertEqual(data["jobs"][0]["state"], "half-paused")
        self.assertIn("Hermes will not run it", " ".join(data["findings"]))

    def test_paused_at_alone_is_a_pause_marker(self):
        _, data = self.payload([job(paused_at="2026-09-01T00:00:00+00:00")])
        self.assertEqual(data["jobs"][0]["state"], "half-paused")

    def test_disabled_without_pause_marker_is_not_a_finding(self):
        result, data = self.payload([job(enabled=False)])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(data["jobs"][0]["state"], "disabled")

    def test_non_object_entries_are_reported(self):
        result, data = self.payload([None, 42, "bad", job()])
        self.assertEqual(result.returncode, 1)
        findings = " ".join(data["findings"])
        for index in ("#0", "#1", "#2"):
            self.assertIn(index, findings)

    def test_wrong_field_types_do_not_crash(self):
        result, _ = self.payload([job(state=[]), job(id="k", schedule={"kind": []}), job(id="s", last_status=[])])
        self.assertIn(result.returncode, (0, 1))

    def test_null_id_is_missing_id(self):
        _, data = self.payload([job(id=None)])
        self.assertIn("missing or invalid id", " ".join(data["findings"]))

    def test_failed_statuses_checked_without_builtin(self):
        for status in ("error", "blocked_config", "interrupted"):
            result, data = self.payload([job(last_status=status, last_error="boom")])
            self.assertEqual(result.returncode, 1, status)
            self.assertIn(status, " ".join(data["findings"]))

    def test_delivery_failed_status_reports_delivery_error(self):
        result, data = self.payload([job(last_status="delivery_failed", last_delivery_error="chat not found")])
        self.assertEqual(result.returncode, 1)
        self.assertIn("not delivered", " ".join(data["findings"]))

    def test_held_is_note_not_finding(self):
        result, data = self.payload([job(last_status="held")])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("held", " ".join(data["notes"]))

    def test_overdue_uses_fifteen_minute_grace(self):
        self.assertEqual(self.run_doctor([job(next_run_at=datetime_iso(-10 * 60))]).returncode, 0)
        result, data = self.payload([job(next_run_at=datetime_iso(-20 * 60))])
        self.assertEqual(result.returncode, 1)
        self.assertIn("overdue", " ".join(data["findings"]))

    def test_naive_next_run_is_note_not_guess(self):
        result, data = self.payload([job(next_run_at="2000-01-01T09:00:00")])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("no timezone", " ".join(data["notes"]))

    def test_failure_streak_is_finding(self):
        result, data = self.payload([job(failure_streak=3)])
        self.assertEqual(result.returncode, 1)
        self.assertIn("3 failed runs", " ".join(data["findings"]))

    def test_thread_note_only_for_chats_with_threads(self):
        direct = job(id="dm", deliver="telegram:42")
        threaded = job(id="topic", deliver="telegram:-1001:7")
        lobby = job(id="lobby", deliver="telegram:-1001")
        _, data = self.payload([direct, threaded, lobby])
        notes = " ".join(data["notes"])
        self.assertIn("lobby", notes)
        self.assertNotIn('dm "', notes)

    def test_pinned_model_is_note(self):
        _, data = self.payload([job(model="model-x", provider="provider-y")])
        self.assertIn("pinned model (model-x · provider-y)", " ".join(data["notes"]))
        _, data = self.payload([job(model="model-x", no_agent=True, script="ping.py")])
        self.assertNotIn("pinned", " ".join(data["notes"]))

    # --- run history ----------------------------------------------------------------------------

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

    def test_latest_completed_run_is_chosen_by_instant_not_text(self):
        # 17:30+07:00 is 10:30 UTC — older than 11:00 UTC, although it sorts later as text.
        self.executions_db(rows=[
            ("a", "daily", "completed", "2026-09-30T17:30:00+07:00", None, None, None, "failed"),
            ("b", "daily", "completed", "2026-09-30T11:00:00+00:00", None, None, None, "delivered"),
            ("c", "daily", "failed", "2026-09-30T12:00:00+00:00", None, None, "boom", None),
        ])
        result, data = self.payload([job()])
        self.assertEqual(result.returncode, 0, data["findings"])

    def test_equal_timestamps_pick_the_later_row(self):
        self.executions_db(rows=[
            ("a", "daily", "completed", "2026-09-30T11:00:00+00:00", None, None, None, "delivered"),
            ("b", "daily", "completed", "2026-09-30T11:00:00+00:00", None, None, None, "failed"),
        ])
        _, data = self.payload([job()])
        self.assertIn("delivery_outcome=failed", " ".join(data["findings"]))

    def test_partial_incident_schema_does_not_hide_delivery_failures(self):
        self.executions_db(
            rows=[("e1", "daily", "completed", "2026-09-30T00:00:00+00:00", None, None, None, "failed")],
            incident_columns="id text")
        result, data = self.payload([job()])
        self.assertEqual(result.returncode, 1)
        self.assertIn("delivery_outcome=failed", " ".join(data["findings"]))
        self.assertIn("unexpected schema", " ".join(data["notes"]))

    def test_unreadable_history_is_finding(self):
        (self.home / "cron" / "executions.db").write_bytes(b"not a database at all")
        result, data = self.payload([job()])
        self.assertEqual(result.returncode, 1)
        self.assertIn("cannot read", " ".join(data["findings"]))

    def test_special_characters_in_home_path(self):
        # "?" is not allowed in Windows file names; the URI escaping is still exercised by "#" and "%".
        special = self.home / ("a #b %c Кир" + ("" if os.name == "nt" else " ?d"))
        (special / "cron").mkdir(parents=True)
        self.home = special
        self.executions_db(rows=[("e1", "daily", "completed", "2026-09-30T00:00:00+00:00", None, None, None, "failed")])
        _, data = self.payload([job()])
        self.assertIn("delivery_outcome=failed", " ".join(data["findings"]))

    # --- --job ----------------------------------------------------------------------------------

    def test_job_detail_shows_runs_delivery_and_next_run(self):
        self.executions_db(rows=[("e1", "daily", "completed", "2026-09-30T00:00:00+00:00", None, None, "", "delivered")])
        result = self.run_doctor([job()], "--job", "daily")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("delivery: delivered", result.stdout)
        self.assertIn("next run: 2099-01-01T09:00:00+00:00", result.stdout)

    def test_job_detail_reports_findings_and_exit_one(self):
        result, data = self.payload([job(state="error", last_error="bad cron")], "--job", "daily")
        self.assertEqual(result.returncode, 1)
        self.assertFalse(data["ok"])
        self.assertIn("state=error", " ".join(data["jobs"][0]["findings"]))

    def test_job_detail_surfaces_history_problems(self):
        (self.home / "cron" / "executions.db").write_bytes(b"not a database at all")
        result, data = self.payload([job()], "--job", "daily")
        self.assertEqual(result.returncode, 1)
        self.assertTrue(data["history_problems"])

    # --- built-in doctor ------------------------------------------------------------------------

    def fake_hermes(self, exit_code):
        record = self.home / "argv.json"
        fake = self.home / "fake-hermes.py"
        fake.write_text(
            "import json, sys\n"
            f"json.dump(sys.argv[1:], open({str(record)!r}, 'w'))\n"
            "print('Cron doctor found 1 issue(s)')\n"
            f"sys.exit({exit_code})\n", encoding="utf-8")
        launcher = self.home / ("fake-hermes.cmd" if os.name == "nt" else "fake-hermes")
        if os.name == "nt":
            launcher.write_text(f'@"{sys.executable}" "{fake}" %*\n', encoding="utf-8")
        else:
            launcher.write_text(f'#!/bin/sh\nexec "{sys.executable}" "{fake}" "$@"\n', encoding="utf-8")
            launcher.chmod(0o755)
        return launcher, record

    def test_builtin_doctor_output_is_included(self):
        launcher, record = self.fake_hermes(1)
        # Without the built-in doctor ours runs the basic checks; with it, it does not duplicate them.
        result, data = self.payload([job(last_status="error")], environment={"HERMES_BIN": str(launcher)})
        self.assertTrue(data["builtin"]["ran"], data["builtin"])
        self.assertEqual(json.loads(record.read_text(encoding="utf-8")), ["cron", "doctor"])
        self.assertEqual(result.returncode, 1)
        findings = " ".join(data["findings"])
        self.assertIn("built-in", findings)
        self.assertNotIn("last run — error", findings)

    def test_missing_cli_falls_back_to_basic_checks(self):
        missing = str(self.home / "no-such-hermes")
        result, data = self.payload([job(last_status="error")], environment={"HERMES_BIN": missing})
        self.assertFalse(data["builtin"]["ran"])
        self.assertIn("last run — error", " ".join(data["findings"]))


if __name__ == "__main__":
    unittest.main()
