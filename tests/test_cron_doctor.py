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

    def test_wal_database_without_shared_memory_is_still_readable(self):
        """Hermes keeps executions.db in WAL mode. SQLite refuses to open a WAL database with
        mode=ro while its -shm file is absent, because read-only connections may not create it.
        The doctor must fall back to a readable mode instead of reporting the history as
        unreadable, which would hide every delivery and failure check."""
        self.executions_db(rows=[("e1", "daily", "failed", datetime_iso(-60), None, None,
                                  "boom", "failed")])
        database = self.home / "cron" / "executions.db"
        connection = sqlite3.connect(database)
        connection.execute("pragma journal_mode=wal")
        connection.execute("pragma wal_checkpoint(truncate)")
        connection.close()
        for suffix in ("-wal", "-shm"):
            leftover = database.with_name(database.name + suffix)
            if leftover.exists():
                leftover.unlink()
        result, data = self.payload([job()])
        self.assertEqual(result.returncode, 0, result.stdout)
        unreadable = [finding for finding in data["findings"] if "unable to open" in finding]
        self.assertEqual(unreadable, [], data["findings"])

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

    def test_summary_counts_jobs_by_state(self):
        jobs = [job(id="a"), job(id="b"), job(id="c", enabled=False, state="paused"),
                job(id="d", enabled=False), job(id="e", enabled=False, state="completed")]
        result, data = self.payload(jobs)
        self.assertEqual(data["summary"]["total"], 5)
        self.assertEqual(data["summary"]["enabled"], 2)
        self.assertEqual(data["summary"]["paused"], 1)
        self.assertEqual(data["summary"]["disabled"], 1)
        self.assertEqual(data["summary"]["completed"], 1)
        human = self.run_doctor(jobs)
        self.assertIn("5 total · 2 enabled · 1 paused · 1 disabled · 1 completed", human.stdout)

    @unittest.skipUnless(os.name == "posix", "needs a POSIX shell pipe")
    def test_closed_pipe_is_quiet(self):
        self.write_jobs([job(id=str(n)) for n in range(300)])
        self.stamp("ticker_heartbeat", f"{time.time()} 1")
        env = os.environ | {"HERMES_HOME": str(self.home)}
        result = subprocess.run(f'"{sys.executable}" "{SCRIPT}" --no-builtin --json | head -c 10 >/dev/null',
                                shell=True, env=env, capture_output=True, text=True)
        self.assertNotIn("Traceback", result.stderr)

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
        for status in ("error", "blocked_config"):
            result, data = self.payload([job(last_status=status, last_error="boom")])
            self.assertEqual(result.returncode, 1, status)
            self.assertIn(status, " ".join(data["findings"]))

    def test_delivery_failed_status_reports_delivery_error(self):
        result, data = self.payload([job(last_status="delivery_failed", last_delivery_error="chat not found")])
        self.assertEqual(result.returncode, 1)
        self.assertIn("not delivered", " ".join(data["findings"]))

    def test_quota_hold_is_note_not_finding(self):
        """Hermes stores a quota hold as last_status=error plus quota_hold_until, never as "held"."""
        held = job(last_status="error", last_error="429 usage limit", quota_hold_until=datetime_iso(3600),
                   next_run_at=datetime_iso(3600))
        result, data = self.payload([held])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("held until", " ".join(data["notes"]))

    def test_expired_quota_hold_is_a_failure_again(self):
        result, data = self.payload([job(last_status="error", last_error="429", quota_hold_until=datetime_iso(-3600))])
        self.assertEqual(result.returncode, 1)
        self.assertIn("last run", " ".join(data["findings"]))

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

    # --- named profiles and the tick error marker ------------------------------------------------

    def profile_home(self, name="work"):
        home = self.home / "profiles" / name
        (home / "cron").mkdir(parents=True)
        (home / "cron" / "jobs.json").write_text(json.dumps({"jobs": [job(id=f"{name}-job", name=f"{name} job")]}),
                                                 encoding="utf-8")
        now = time.time()
        (home / "cron" / "ticker_heartbeat").write_text(f"{now} {os.getpid()}", encoding="utf-8")
        (home / "cron" / "ticker_last_success").write_text(str(now), encoding="utf-8")
        return home

    def test_profile_reads_its_own_store(self):
        self.profile_home()
        result, data = self.payload([job()], "--profile", "Work")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual([item["id"] for item in data["jobs"]], ["work-job"])

    def test_default_profile_is_the_root_home(self):
        _, data = self.payload([job()], "--profile", "default")
        self.assertEqual([item["id"] for item in data["jobs"]], ["daily"])

    def test_unknown_or_invalid_profile_is_exit_three(self):
        self.assertEqual(self.run_doctor([job()], "--profile", "missing").returncode, 3)
        self.assertEqual(self.run_doctor([job()], "--profile", "../x").returncode, 3)

    def test_profile_is_passed_to_builtin_doctor(self):
        self.profile_home()
        launcher, record = self.fake_hermes(0)
        self.payload([job()], "--profile", "work", environment={"HERMES_BIN": str(launcher)})
        self.assertEqual(json.loads(record.read_text(encoding="utf-8")), ["--profile", "work", "cron", "doctor"])

    def test_profile_resolves_from_root_when_home_is_a_profile(self):
        """HERMES_HOME may already point at a profile: --profile still means a sibling or the root."""
        work = self.profile_home("work")
        self.profile_home("other")
        env = {"HERMES_HOME": str(work)}
        _, data = self.payload([job()], "--profile", "other", environment=env)
        self.assertEqual([item["id"] for item in data["jobs"]], ["other-job"])
        _, data = self.payload([job()], "--profile", "default", environment=env)
        self.assertEqual([item["id"] for item in data["jobs"]], ["daily"])

    def stamps(self, success_ago, error_ago=None, error_text="sqlite3.OperationalError: database is locked"):
        """Fixed stamps with a guaranteed order; the doctor runs with heartbeat=False so none is rewritten."""
        now = time.time()
        self.write_jobs([job()])
        self.stamp("ticker_heartbeat", f"{now} {os.getpid()}")
        self.stamp("ticker_last_success", str(now - success_ago))
        if error_ago is not None:
            self.stamp("ticker_last_error", f"{now - error_ago}\n{error_text}\n")

    def test_failed_last_tick_is_finding(self):
        self.stamps(success_ago=120, error_ago=60)
        result, data = self.payload([job()], write=False, heartbeat=False)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("the last tick failed (60 s ago)", " ".join(data["findings"]))
        self.assertIn("database is locked", data["scheduler"]["last_tick_error"])

    def test_tick_error_older_than_last_success_is_ignored(self):
        self.stamps(success_ago=60, error_ago=600, error_text="old failure")
        result, data = self.payload([job()], write=False, heartbeat=False)
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_unreadable_or_empty_tick_error_is_finding(self):
        self.stamps(success_ago=60)
        (self.home / "cron" / "ticker_last_error").mkdir()  # cannot be read as a file
        result, data = self.payload([job()], write=False, heartbeat=False)
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("cannot read ticker_last_error", " ".join(data["findings"]))
        (self.home / "cron" / "ticker_last_error").rmdir()
        self.stamp("ticker_last_error", f"{time.time()}\n")
        _, data = self.payload([job()], write=False, heartbeat=False)
        self.assertIn("holds no message", " ".join(data["findings"]))

    # --- checks the built-in doctor makes, repeated for --job and without it ---------------------

    def test_crashed_builtin_doctor_is_finding(self):
        launcher, _ = self.fake_hermes(2)
        result, data = self.payload([job()], environment={"HERMES_BIN": str(launcher)})
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertFalse(data["builtin"]["ran"])
        self.assertIn("built-in `hermes cron doctor` failed (exit code 2", " ".join(data["findings"]))

    def test_active_one_shot_without_next_run_is_finding(self):
        once = job(schedule={"kind": "once", "run_at": "2099-01-01T09:00:00+00:00"}, next_run_at=None)
        _, data = self.payload([once])
        self.assertIn("active job has no next_run_at", " ".join(data["findings"]))

    def test_job_detail_checks_script_workdir_and_fire_error(self):
        (self.home / "scripts").mkdir()
        (self.home / "scripts" / "ok.py").write_text("print('x')\n", encoding="utf-8")
        broken = job(script="missing.py", workdir=str(self.home / "no-such-dir"),
                     last_fire_error={"at": "2026-10-14T03:00:00+00:00", "detail": "worker exited before ack"})
        result = self.run_doctor([broken], "--job", "daily")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("script not found", result.stdout)
        self.assertIn("workdir not found", result.stdout)
        self.assertIn("last fire did not dispatch — worker exited before ack", result.stdout)
        result = self.run_doctor([job(script="ok.py")], "--job", "daily")
        self.assertNotIn("script", " ".join(line for line in result.stdout.splitlines() if "🔴" in line))

    def test_script_outside_scripts_dir_is_finding(self):
        (self.home / "scripts").mkdir()
        _, data = self.payload([job(script="../outside.py")])
        self.assertIn("script resolves outside", " ".join(data["findings"]))

    def test_unopenable_executions_db_is_finding(self):
        (self.home / "cron" / "executions.db").mkdir()  # exists, but is not a database
        result, data = self.payload([job()])
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("executions", " ".join(data["findings"]))

    def test_missing_executions_db_is_only_a_note(self):
        result, data = self.payload([job()])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("executions.db not found", " ".join(data["notes"]))

    def test_read_only_tick_lock_makes_store_unwritable(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cron_doctor", SCRIPT)
        doctor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doctor)
        lock = self.home / "cron" / ".tick.lock"
        lock.write_text("", encoding="utf-8")
        lock.chmod(0o444)
        try:
            if os.access(lock, os.W_OK):
                self.skipTest("running as a user that ignores file modes")
            self.assertIn("not writable", " ".join(doctor.store_health(self.home)))
        finally:
            lock.chmod(0o644)

    def test_null_name_prints_as_question_mark(self):
        self.executions_db(incidents=[("i1", "daily", "sig", "alerted", "boom", datetime_iso(-60), datetime_iso(-60))])
        _, data = self.payload([job(name=None)])
        findings = " ".join(data["findings"])
        self.assertIn('daily "?"', findings)
        self.assertNotIn('"None"', findings)

    def test_missing_cli_falls_back_to_basic_checks(self):
        missing = str(self.home / "no-such-hermes")
        result, data = self.payload([job(last_status="error")], environment={"HERMES_BIN": missing})
        self.assertFalse(data["builtin"]["ran"])
        self.assertIn("last run — error", " ".join(data["findings"]))

    DISPATCH = ("Restart-safe cron worker dispatch failed: cron external worker exited before "
                "ownership acknowledgement (exit 1) [execution {}]")

    def failed_runs(self, specs):
        """(job_id, seconds ago, error) → failed execution rows; the error may hold a per-run id."""
        rows = []
        for index, (job_id, ago, error) in enumerate(specs):
            moment = datetime_iso(-ago)
            rows.append((f"r{index}", job_id, "failed", moment, moment, moment,
                         error.format(f"{index:04d}abcdef{index:04d}"), None))
        return rows

    def test_same_error_in_three_jobs_is_one_finding(self):
        """The 0.21.3 outage shape: one dispatch error across jobs, all other fields look healthy."""
        self.executions_db(rows=self.failed_runs(
            [(name, ago, self.DISPATCH) for name in ("daily", "tick", "backup") for ago in (300, 1800)]))
        result, data = self.payload([job(), job(id="tick", name="Tick"), job(id="backup", name="Backup")])
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertEqual(len(data["shared_failures"]), 1)
        group = data["shared_failures"][0]
        self.assertEqual((group["jobs"], group["runs"]), (["backup", "daily", "tick"], 6))
        findings = " ".join(data["findings"])
        self.assertIn("the same error failed 3 jobs", findings)
        self.assertIn("ownership acknowledgement", findings)

    def test_two_jobs_old_runs_and_different_errors_are_not_shared(self):
        self.executions_db(rows=self.failed_runs([
            ("daily", 300, self.DISPATCH), ("tick", 300, self.DISPATCH),          # only two jobs
            ("a", 2 * 86400, "disk full"), ("b", 2 * 86400, "disk full"), ("c", 2 * 86400, "disk full"),
            ("x", 600, "provider 500"), ("y", 600, "timeout"), ("z", 600, "bad key"),
        ]))
        result, data = self.payload([job()])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(data["shared_failures"], [])

    def test_restart_interruptions_are_not_a_fault(self):
        self.executions_db(rows=self.failed_runs(
            [(name, 120, "Interrupted by shutdown before terminal completion.")
             for name in ("daily", "tick", "backup")]))
        self.assertEqual(self.payload([job()])[1]["shared_failures"], [])

    def test_gateway_shutdown_texts_are_not_a_fault(self):
        """The exact texts Hermes 0.22.2 writes when a gateway restart interrupts running jobs."""
        for text in ("Interrupted by gateway shutdown before terminal completion.",
                     "Interrupted by gateway shutdown before the run finished (tool subprocess was killed mid-flight).",
                     "Gateway shutdown (drain) killed the job's tool subprocess before the run finished."):
            self.executions_db(rows=self.failed_runs([(name, 120, text) for name in ("daily", "tick", "backup")]))
            self.assertEqual(self.payload([job()])[1]["shared_failures"], [], text)
            (self.home / "cron" / "executions.db").unlink()

    def test_executions_without_error_column_is_a_note(self):
        connection = sqlite3.connect(self.home / "cron" / "executions.db")
        connection.execute("create table executions (id text, job_id text, status text, claimed_at text)")
        connection.commit()
        connection.close()
        result, data = self.payload([job()])
        self.assertIn("shared failures not checked", " ".join(data["notes"]))

    def test_measured_durations_do_not_split_one_fault(self):
        """Hermes 0.21.6 masks durations in incident signatures; the doctor groups the same way."""
        self.executions_db(rows=self.failed_runs([
            ("daily", 300, "worker idle for 603s, killed"), ("tick", 300, "worker idle for 612s, killed"),
            ("backup", 300, "worker idle for 1.5 minutes, killed")]))
        result, data = self.payload([job(), job(id="tick", name="Tick"), job(id="backup", name="Backup")])
        self.assertEqual(len(data["shared_failures"]), 1, result.stdout)

    # --- Hermes 0.21.6: sticky last_failure, cron store health ---------------------------------

    def test_recovered_last_failure_is_note(self):
        failure = {"at": "2026-10-07T03:00:00+00:00", "detail": "provider timeout"}
        result, data = self.payload([job(last_failure=failure)])
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("last failure at 2026-10-07T03:00:00+00:00: provider timeout — recovered since",
                      " ".join(data["notes"]))
        self.assertEqual(data["jobs"][0]["last_failure"], failure)

    def test_last_failure_of_failing_job_is_not_repeated(self):
        failure = {"at": "2026-10-07T03:00:00+00:00", "detail": "provider timeout"}
        _, data = self.payload([job(last_status="error", last_error="provider timeout", last_failure=failure)])
        self.assertNotIn("recovered since", " ".join(data["notes"]))

    def test_job_detail_shows_last_failure(self):
        failure = {"at": "2026-10-07T03:00:00+00:00", "detail": "provider timeout"}
        result = self.run_doctor([job(last_failure=failure)], "--job", "daily")
        self.assertIn("last failure: 2026-10-07T03:00:00+00:00 — provider timeout", result.stdout)

    def test_store_findings(self):
        import importlib.util
        spec = importlib.util.spec_from_file_location("cron_doctor", SCRIPT)
        doctor = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(doctor)
        store = self.home / "cron"
        self.assertEqual(doctor.store_findings(store, True, 10 << 30), [])
        self.assertIn("not writable", doctor.store_findings(store, False, 10 << 30)[0])
        self.assertIn("only 50 MB free", doctor.store_findings(store, True, 50 << 20)[0])
        self.assertEqual(doctor.store_findings(store, True, None), [])
        self.assertEqual(doctor.store_health(self.home), [])  # a writable temp dir with free space

    # --- Hermes 0.22.4: auto-pause, headless tick, settled deliveries, command-line scripts ------

    # Exact engine texts (v0.22.4 cron/scheduler_authority.py, scheduler_gateway_gate.py,
    # scheduler_delivery.py); the job name and receipt are made up.
    AUTO_PAUSES = (
        "cron admission identity conflict",
        "Cron execution unverified; reconcile /home/u/.hermes/cron/admissions/ab12.json: "
        "unknown_execution: cron admission was not replayed",
        "unknown_execution: cron admission was not replayed",
        "Canonical cron unknown; no automatic re-execution",
    )
    SKIP_ABSENT = ("Skipped: the gateway is not running, so agent jobs cannot fire; they run at the next tick "
                   "once it is up. Start it with `hermes gateway start` (or `hermes gateway install` for a "
                   "service).")
    SKIP_STARTING = ("Skipped: the gateway is starting (control_socket_timeout), so agent jobs cannot fire; they "
                     "run at the next tick once it is ready. Check it with `hermes gateway status`.")
    PARKED = ("bot-chat:work queued (receipt 3f2a9c1d7e44): target profile not running; admitted once it is")

    def auto_paused(self, reason, **overrides):
        return job(enabled=False, state="paused", paused_at="2026-10-22T03:00:00+07:00",
                   paused_reason=reason, **overrides)

    def test_hermes_auto_pause_is_finding(self):
        for reason in self.AUTO_PAUSES:
            result, data = self.payload([self.auto_paused(reason)])
            findings = " ".join(data["findings"])
            self.assertEqual(result.returncode, 1, reason)
            self.assertIn("Hermes paused this job after a fire it could not verify", findings, reason)
            self.assertIn("hermes cron runs daily", findings, reason)

    def test_auto_paused_past_one_shot_mentions_resume_at(self):
        once = self.auto_paused(self.AUTO_PAUSES[3], schedule={"kind": "once", "run_at": datetime_iso(-7200)},
                                next_run_at=None)
        _, data = self.payload([once])
        self.assertIn("hermes cron resume daily --at", " ".join(data["findings"]))
        _, data = self.payload([self.auto_paused(self.AUTO_PAUSES[3])])  # recurring: no one-shot advice
        self.assertNotIn("--at <ISO>", " ".join(data["findings"]))

    def test_operator_pause_stays_silent(self):
        """Regression guard: a pause a person set, with any reason or none, is not a finding."""
        for reason in ("holiday", "", None, 123, ["unknown_execution: x"], "paused after unknown_execution: x"):
            result, data = self.payload([self.auto_paused(reason)])
            self.assertEqual(result.returncode, 0, (reason, data["findings"]))

    def skipped(self, job_id, detail, **overrides):
        fields = {"next_run_at": datetime_iso(-3600), **overrides}
        return job(id=job_id, name=job_id.title(), last_fire_error={"at": datetime_iso(-60), "detail": detail},
                   **fields)

    def test_gateway_skip_in_two_jobs_is_one_finding(self):
        jobs = [self.skipped("a", self.SKIP_ABSENT), self.skipped("b", self.SKIP_STARTING),
                job(id="watch", name="Watch", no_agent=True, script="w.sh")]
        (self.home / "scripts").mkdir()
        (self.home / "scripts" / "w.sh").write_text("true\n", encoding="utf-8")
        result, data = self.payload(jobs)
        findings = data["findings"]
        joined = " ".join(findings)
        self.assertEqual(result.returncode, 1)
        grouped = [item for item in findings if "agent jobs were skipped because the gateway was not ready" in item]
        self.assertEqual(len(grouped), 1, findings)
        self.assertIn("2 agent jobs", grouped[0])
        self.assertIn("not running", grouped[0])
        self.assertIn("starting (control_socket_timeout)", grouped[0])
        self.assertNotIn("last fire did not dispatch", joined)
        self.assertNotIn("overdue", joined)
        self.assertEqual(data["skipped_fires"]["jobs"], ["a", "b"])
        # With the built-in doctor ours adds the same single finding.
        launcher, _ = self.fake_hermes(1)
        _, data = self.payload(jobs, environment={"HERMES_BIN": str(launcher)})
        self.assertEqual(data["skipped_fires"]["jobs"], ["a", "b"])
        self.assertEqual(sum("agent jobs were skipped" in item for item in data["findings"]), 1)
        # --job shows the group instead of a lone overdue job.
        result, data = self.payload(jobs, "--job", "a")
        detail = " ".join(data["jobs"][0]["findings"])
        self.assertIn("agent jobs were skipped because the gateway was not ready", detail)
        self.assertNotIn("overdue", detail)

    def test_single_or_unrelated_fire_errors_are_not_grouped(self):
        """Regression guard: one skipped job keeps its own finding; other fire errors never group."""
        lone = self.skipped("a", self.SKIP_ABSENT, next_run_at="2099-01-01T09:00:00+00:00")
        result, data = self.payload([lone])
        self.assertIn("last fire did not dispatch — Skipped: the gateway is not running", " ".join(data["findings"]))
        self.assertIsNone(data.get("skipped_fires"))
        other = [self.skipped(name, "worker exited before ack", next_run_at="2099-01-01T09:00:00+00:00")
                 for name in ("a", "b", "c")]
        _, data = self.payload(other)
        self.assertIsNone(data.get("skipped_fires"))
        self.assertEqual(sum("last fire did not dispatch" in item for item in data["findings"]), 3)
        duplicate = [lone, dict(lone)]  # one id twice is one job, not two
        _, data = self.payload(duplicate)
        self.assertIsNone(data.get("skipped_fires"))
        for broken in ("Skipped: the gateway is not running", ["x"], {"detail": 5}, {"detail": None}):
            result, data = self.payload([job(last_fire_error=broken), job(id="b", last_fire_error=broken)])
            self.assertNotIn("Traceback", result.stderr)
            self.assertIsNone(data.get("skipped_fires"))

    def queued_run(self, job_id="daily", status="completed", claimed=-7200, finished=-7000, outcome="queued"):
        claimed_at = claimed if isinstance(claimed, (str, type(None))) else datetime_iso(claimed)
        finished_at = finished if isinstance(finished, (str, type(None))) else datetime_iso(finished)
        return ("q-" + status, job_id, status, claimed_at, claimed_at, finished_at, None, outcome)

    def test_stale_queued_delivery_is_note(self):
        for status in ("completed", "failed", "unknown"):
            self.executions_db(rows=[self.queued_run(status=status)])
            result, data = self.payload([job()])
            self.assertEqual(result.returncode, 0, (status, data["findings"]))
            self.assertIn("still queued", " ".join(data["notes"]), status)
            result = self.run_doctor([job()], "--job", "daily")
            self.assertIn("still queued", result.stdout, status)
            (self.home / "cron" / "executions.db").unlink()

    def test_fresh_or_unjudgeable_queued_delivery_is_silent(self):
        """Regression guard: a fresh, long-running, naive, future or broken stamp is never judged stale."""
        for claimed, finished in ((-120, -60), (-7200, -60), ("2026-01-01T00:00:00", None),
                                  (3600 * 5, None), ("garbage", "garbage"), (None, None)):
            self.executions_db(rows=[self.queued_run(claimed=claimed, finished=finished)])
            result, data = self.payload([job()])
            self.assertEqual(result.returncode, 0, (claimed, finished, data["findings"]))
            self.assertNotIn("still queued", " ".join(data["notes"]), (claimed, finished))
            (self.home / "cron" / "executions.db").unlink()

    def test_command_line_script_hint(self):
        (self.home / "scripts").mkdir()
        for script in ("echo hi", "df -h\t/", "a\nb"):
            _, data = self.payload([job(script=script)])
            self.assertIn("looks like a command line", " ".join(data["findings"]), repr(script))

    def test_real_scripts_are_not_command_lines(self):
        """Regression guard: an existing file with a space, a plain missing name, a directory, a path outside."""
        scripts = self.home / "scripts"
        scripts.mkdir()
        (scripts / "my check.sh").write_text("true\n", encoding="utf-8")
        (scripts / "adir").mkdir()
        result, data = self.payload([job(script="my check.sh")])
        self.assertEqual(result.returncode, 0, data["findings"])
        for script, expected in (("missing.py", "script not found"), ("adir", "not a file"),
                                 ("../out side.py", "script resolves outside")):
            _, data = self.payload([job(script=script)])
            findings = " ".join(data["findings"])
            self.assertIn(expected, findings, script)
            self.assertNotIn("command line", findings, script)
            self.assertNotIn("Path(", findings, script)  # a plain path, never a repr

    def test_parked_bot_chat_delivery_is_note(self):
        parked = job(deliver="bot-chat:work", last_status="delivery_failed", last_delivery_error=self.PARKED)
        self.executions_db(rows=[("e1", "daily", "completed", datetime_iso(-600), None, datetime_iso(-590),
                                  None, "failed")])
        result, data = self.payload([parked])
        self.assertEqual(result.returncode, 0, data["findings"])
        self.assertIn("parked until the target profile's gateway runs", " ".join(data["notes"]))
        # A real delivery failure next to it is still a finding.
        _, data = self.payload([dict(parked, last_delivery_error="chat not found")])
        self.assertIn("not delivered", " ".join(data["findings"]))

    def test_host_store_errors_are_shared_fault(self):
        """Regression guard: 0.22.4 host errors are real faults, never treated as restarts."""
        for error in ("Canonical cron session store acquisition timed out",
                      "Cron admission missing: the owner never admitted this fire"):
            self.executions_db(rows=self.failed_runs([(name, 300, error) for name in ("daily", "tick", "backup")]))
            _, data = self.payload([job()])
            self.assertEqual(len(data["shared_failures"]), 1, error)
            (self.home / "cron" / "executions.db").unlink()

    def test_new_job_fields_do_not_crash(self):
        """Regression guard: 0.22.4 bookkeeping fields, well-formed or broken."""
        for extra in ({"canonical_completions": ["e1", "e2"], "pending_slot": "2026-10-22T09:00:00+07:00",
                       "last_delivery_queued": {"canonical": {"status": "queued", "execution_id": "e2"}}},
                      {"canonical_completions": "e1", "pending_slot": 5, "last_delivery_queued": ["x"]}):
            result, data = self.payload([job(**extra)])
            self.assertEqual(result.returncode, 0, (extra, data["findings"]))


if __name__ == "__main__":
    unittest.main()
