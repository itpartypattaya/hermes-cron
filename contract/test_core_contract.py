"""Contract check against a real Hermes core: the core's own code writes the cron store, the doctor reads it.

The unit tests in tests/ build jobs.json and executions.db by hand, so a format change in Hermes would leave
them green while the doctor stops seeing what it looks for. Here `hermes cron create / pause / run` and the
core's ticker functions write a real store (no-agent script jobs: no network, no model keys), and the doctor
must read all of it: a healthy store gives no findings, a broken one gives exactly the findings its faults
call for.

Needs Hermes core importable by this interpreter (`python -m hermes_cli.main`) and the `hermes` CLI on PATH
or in HERMES_BIN; CI installs the latest core release (the `core-contract` job). Not part of
`python -m unittest discover -s tests`: run `python -m unittest discover -s contract -v`.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path


DOCTOR = Path(__file__).parents[1] / "skills" / "hermes-cron" / "scripts" / "cron-doctor.py"
FAIL_ERROR = "disk check failed"


def environment(home):
    env = dict(os.environ, HERMES_HOME=str(home), PYTHONIOENCODING="utf-8", NO_COLOR="1")
    env.pop("HERMES_SESSION_KEY", None)
    return env


def core(home, *args, ok=(0,)):
    """`hermes <args>` through this interpreter, so the core under test is the one importable here."""
    result = subprocess.run([sys.executable, "-m", "hermes_cli.main", *args], env=environment(home),
                            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=300)
    if result.returncode not in ok:
        raise AssertionError(f"hermes {' '.join(args)} failed ({result.returncode}):\n"
                             f"{result.stdout}\n{result.stderr}")
    return result.stdout


def core_code(home, code):
    """Run `code` with the core's modules, bound to `home` like a gateway process of that profile."""
    result = subprocess.run([sys.executable, "-c", code], env=environment(home), capture_output=True,
                            text=True, encoding="utf-8", errors="replace", timeout=120)
    if result.returncode != 0:
        raise AssertionError(f"core code failed ({result.returncode}):\n{code}\n{result.stdout}\n{result.stderr}")


def create(home, name, script, deliver="local", *extra):
    output = core(home, "cron", "create", "every 1h", "--name", name, "--no-agent", "--script", script,
                  "--deliver", deliver, *extra)
    match = re.search(r"Created job: (\S+)", output)
    if not match:
        raise AssertionError(f"no job id in `hermes cron create` output:\n{output}")
    return match.group(1)


def new_home():
    home = Path(tempfile.mkdtemp(prefix="cron-contract-"))
    (home / "scripts").mkdir()
    (home / "scripts" / "hello.py").write_text('print("report ready")\n', encoding="utf-8")
    (home / "scripts" / "fail.py").write_text(
        f'import sys\nprint("{FAIL_ERROR}", file=sys.stderr)\nsys.exit(3)\n', encoding="utf-8")
    return home


def doctor(home, *args, builtin=False):
    command = [sys.executable, str(DOCTOR), "--json", *args, *([] if builtin else ["--no-builtin"])]
    result = subprocess.run(command, env=environment(home), capture_output=True, text=True, encoding="utf-8",
                            errors="replace", timeout=300)
    try:
        data = json.loads(result.stdout)
    except ValueError:
        raise AssertionError(f"doctor printed no JSON ({result.returncode}):\n{result.stdout}\n{result.stderr}")
    return result.returncode, data


def hermes_binary():
    return os.environ.get("HERMES_BIN") or shutil.which("hermes")


class HealthyStore(unittest.TestCase):
    """A store with nothing wrong in it: everything read, nothing reported."""

    @classmethod
    def setUpClass(cls):
        cls.home = new_home()
        cls.ok_id = create(cls.home, "report", "hello.py")
        cls.paused_id = create(cls.home, "on hold", "hello.py")
        core(cls.home, "cron", "run", cls.ok_id)
        core(cls.home, "cron", "pause", cls.paused_id)
        # What the gateway ticker writes after a good tick.
        core_code(cls.home, "from cron.jobs import record_ticker_heartbeat; record_ticker_heartbeat(success=True)")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def test_store_journal_mode_is_one_the_doctor_reads(self):
        # Hermes creates a new database in WAL, except with an SQLite that has the WAL-reset bug (before 3.51.3,
        # as on the Linux CI runner): then in DELETE. A database that is already WAL stays WAL either way.
        with sqlite3.connect(self.home / "cron" / "executions.db") as connection:
            self.assertIn(connection.execute("pragma journal_mode").fetchone()[0], ("wal", "delete"))

    def test_no_findings_and_everything_read(self):
        code, data = doctor(self.home)
        self.assertEqual(data["findings"], [], data)
        self.assertEqual(code, 0)
        self.assertEqual(data["notes"], [
            "built-in doctor not run (disabled with --no-builtin) — basic checks were done here"])
        self.assertEqual(data["summary"], {"total": 2, "enabled": 1, "half-paused": 0, "paused": 1,
                                           "disabled": 0, "completed": 0, "error": 0})
        states = {job["id"]: (job["name"], job["state"], job["last_status"]) for job in data["jobs"]}
        self.assertEqual(states, {self.ok_id: ("report", "enabled", "ok"),
                                  self.paused_id: ("on hold", "paused", None)})
        scheduler = data["scheduler"]
        self.assertLess(scheduler["heartbeat_age_seconds"], 120)
        self.assertLess(scheduler["success_age_seconds"], 120)
        self.assertIsInstance(scheduler["heartbeat_pid"], int)
        self.assertIsNone(scheduler["last_tick_error"])

    def test_run_history_of_a_job(self):
        code, data = doctor(self.home, "--job", self.ok_id)
        self.assertEqual(code, 0, data)
        self.assertEqual((data["history_problems"], data["history_notes"]), ([], []))
        [detail] = data["jobs"]
        [run] = detail["runs"]
        self.assertEqual(run["status"], "completed")
        self.assertEqual(run["delivery_outcome"], "suppressed")  # deliver=local
        self.assertEqual(run["error"], "")
        for field in ("claimed_at", "started_at", "finished_at"):
            self.assertRegex(run[field] or "", r"^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}", field)

    def test_with_the_builtin_doctor(self):
        binary = hermes_binary()
        self.assertIsNotNone(binary, "hermes CLI not found: put it on PATH or set HERMES_BIN")
        code, data = doctor(self.home, builtin=True)
        self.assertTrue(data["builtin"]["ran"], data["builtin"])
        self.assertEqual(data["builtin"]["exit_code"], 0, data["builtin"]["output"])
        self.assertEqual((code, data["findings"]), (0, []), data)


class BrokenStore(unittest.TestCase):
    """A store with real faults, written the way Hermes writes them: each must show, nothing else."""

    @classmethod
    def setUpClass(cls):
        cls.home = new_home()
        cls.failing = [create(cls.home, f"fails {n}", "fail.py") for n in (1, 2, 3)]
        cls.undelivered = create(cls.home, "undelivered", "hello.py", "telegram:12345")
        for job_id in cls.failing + [cls.failing[0], cls.undelivered]:
            core(cls.home, "cron", "run", job_id, ok=(0, 1))  # 1: the run failed, as it should
        core_code(cls.home, "from cron.jobs import record_ticker_heartbeat, record_ticker_error; "
                            "record_ticker_heartbeat(success=True); record_ticker_error('contract tick error')")

    @classmethod
    def tearDownClass(cls):
        shutil.rmtree(cls.home, ignore_errors=True)

    def expected(self):
        first = self.failing[0]
        patterns = [
            r"the last tick failed \(.+\): contract tick error",
            rf"the same error failed 3 jobs in the last 24 h \(4 runs, .+\).+{FAIL_ERROR}",
            rf'{first} "fails 1": 2 failed runs in a row',
            rf'{self.undelivered} "undelivered": result not delivered — .*telegram',
            rf'{self.undelivered} "undelivered": latest completed run \(.+\) was not delivered '
            r"\(delivery_outcome=(failed|not_configured)\)",
        ]
        for n, job_id in enumerate(self.failing, 1):
            patterns += [rf'{job_id} "fails {n}": last run — error: .*{FAIL_ERROR}',
                         rf'open incident \S+ — {job_id} "fails {n}": .*{FAIL_ERROR}']
        return patterns

    def test_each_fault_is_found_once_and_nothing_else(self):
        code, data = doctor(self.home)
        self.assertEqual(code, 1, data)
        findings = data["findings"]
        for pattern in self.expected():
            hits = [item for item in findings if re.search(pattern, item, re.DOTALL)]
            self.assertEqual(len(hits), 1, f"{pattern!r} in {json.dumps(findings, indent=1)}")
        unexpected = [item for item in findings
                      if not any(re.search(pattern, item, re.DOTALL) for pattern in self.expected())]
        self.assertEqual(unexpected, [])
        self.assertEqual(data["notes"], [
            "built-in doctor not run (disabled with --no-builtin) — basic checks were done here"])
        self.assertEqual(sorted(item["job_id"] for item in data["incidents"]), sorted(self.failing))
        self.assertEqual(data["scheduler"]["last_tick_error"], "contract tick error")

    def test_failed_runs_of_a_job(self):
        code, data = doctor(self.home, "--job", self.failing[0])
        self.assertEqual(code, 1)
        self.assertEqual((data["history_problems"], data["history_notes"]), ([], []))
        runs = data["jobs"][0]["runs"]
        self.assertEqual([run["status"] for run in runs], ["failed", "failed"])
        self.assertTrue(all(FAIL_ERROR in run["error"] for run in runs), runs)
        self.assertGreater(runs[0]["claimed_at"], runs[1]["claimed_at"])  # newest first


class HistoryStillInWal(unittest.TestCase):
    """A long-running gateway keeps executions.db open, so the latest runs live in -wal for a while."""

    def test_latest_run_is_read_from_the_wal(self):
        home = new_home()
        self.addCleanup(shutil.rmtree, home, True)
        job_id = create(home, "report", "hello.py")
        core(home, "cron", "run", job_id)  # creates the database
        database = home / "cron" / "executions.db"
        # A store that is WAL already (made by an SQLite without the WAL-reset bug, as in production): Hermes
        # never switches it back, whatever SQLite it runs on now.
        with sqlite3.connect(database) as connection:
            self.assertEqual(connection.execute("pragma journal_mode=wal").fetchone()[0], "wal")
        connection.close()
        holder = sqlite3.connect(database)  # another open connection: the core's close does not checkpoint
        self.addCleanup(holder.close)
        holder.execute("select count(*) from executions").fetchone()
        core(home, "cron", "run", job_id)
        wal = database.with_name(database.name + "-wal")
        self.assertGreater(wal.stat().st_size if wal.exists() else 0, 0, "the second run is not in -wal")
        self.assertEqual(holder.execute("pragma journal_mode").fetchone()[0], "wal", "Hermes switched WAL off")
        code, data = doctor(home, "--job", job_id)
        self.assertEqual(code, 0, data)
        self.assertEqual((data["history_problems"], data["history_notes"]), ([], []))
        self.assertEqual([run["status"] for run in data["jobs"][0]["runs"]], ["completed", "completed"])


if __name__ == "__main__":
    unittest.main()
