import importlib.util
import json
import os
import subprocess
import sys
import tempfile
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
    }
    value.update(overrides)
    return value


class CronDoctorTests(unittest.TestCase):
    def run_doctor(self, jobs, *arguments):
        with tempfile.TemporaryDirectory() as temporary:
            home = Path(temporary)
            cron = home / "cron"
            cron.mkdir()
            (cron / "jobs.json").write_text(json.dumps({"jobs": jobs}), encoding="utf-8")
            (cron / "ticker_heartbeat").write_text(str(__import__("time").time()), encoding="utf-8")
            (cron / "ticker_last_success").write_text(str(__import__("time").time()), encoding="utf-8")
            environment = os.environ | {"HERMES_HOME": str(home)}
            return subprocess.run(
                [sys.executable, str(SCRIPT), *arguments],
                env=environment,
                text=True,
                capture_output=True,
                encoding="utf-8",
            )

    def test_healthy_job_is_zero_and_json(self):
        result = self.run_doctor([job()], "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        payload = json.loads(result.stdout)
        self.assertTrue(payload["ok"])
        self.assertEqual(payload["jobs"][0]["state"], "enabled")

    def test_detects_contradictory_pause(self):
        result = self.run_doctor([job(state="paused")], "--json")
        self.assertEqual(result.returncode, 1)
        self.assertIn("enabled=true при state=paused", result.stdout)

    def test_detects_bare_duration_as_unknown_only_when_runtime_is_invalid(self):
        result = self.run_doctor([job(schedule={"kind": "interval", "interval": "30m"})], "--json")
        self.assertEqual(result.returncode, 0, result.stdout)

    def test_works_without_mia_snapshot_scripts(self):
        result = self.run_doctor([job()], "--json")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertNotIn("cron-snapshot.py", result.stderr)

    def test_missing_runtime_is_exit_two(self):
        with tempfile.TemporaryDirectory() as temporary:
            environment = os.environ | {"HERMES_HOME": temporary}
            result = subprocess.run([sys.executable, str(SCRIPT), "--json"], env=environment,
                                    text=True, capture_output=True, encoding="utf-8")
        self.assertEqual(result.returncode, 2)
        self.assertIn("jobs.json", result.stdout)


if __name__ == "__main__":
    unittest.main()
