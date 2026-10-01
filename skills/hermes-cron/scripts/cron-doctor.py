#!/usr/bin/env python3
"""Read-only health check for a standard Hermes cron installation.

Complements the built-in `hermes cron doctor` instead of duplicating it: runs it when the CLI is
available, then adds what it does not look at — ticker stamps, disabled and paused jobs, duplicate ids, half-paused records, consecutive failures,
delivery outcomes from executions.db, open failure incidents, pinned models and thread-less
delivery into chats that use threads. Without the CLI (or with --no-builtin) the basic checks the
built-in would have made — failed last run, undelivered result, overdue next_run_at — run here.

The doctor itself never ticks, executes or changes a job. The built-in doctor is Hermes code: its
job loader, like every scheduler tick, may repair malformed entries in jobs.json.

No custom sync or deployment layer is assumed. Exit codes: 0 healthy, 1 findings, 2 unreadable
runtime state, 3 invalid arguments or an unknown job. Use --json for automation.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


HEARTBEAT_LIMIT = 180        # the ticker writes a stamp once a minute
SUCCESS_LIMIT = 600
OVERDUE_GRACE = 15 * 60      # same grace as `hermes cron doctor` and `cron status`
FAILED_STATUSES = {"error", "blocked_config", "interrupted"}
BAD_DELIVERY = {"failed", "not_configured"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or os.environ.get("HERMES_DIR") or "~/.hermes").expanduser()


def load_json(path: Path):
    try:
        with path.open(encoding="utf-8-sig") as stream:
            return json.load(stream), None
    except FileNotFoundError:
        return None, f"missing {path}"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"cannot read {path}: {exc}"


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def read_stamp(path: Path):
    """(epoch, pid) from a ticker stamp. Hermes 0.21.5 writes `<epoch> <pid>`, older versions `<epoch>`."""
    try:
        fields = path.read_text(encoding="utf-8").split()
        epoch = float(fields[0])
    except (OSError, ValueError, IndexError):
        return None, None
    pid = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
    return epoch, pid


def describe_age(seconds):
    if seconds is None:
        return "no stamp"
    if seconds < 90:
        return f"{int(seconds)} s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    return f"{int(seconds // 3600)} h ago"


def has_pause_marker(job) -> bool:
    return job.get("state") == "paused" or bool(job.get("paused_at"))


def job_state(job):
    enabled = job.get("enabled", True)
    if enabled and has_pause_marker(job):
        return "half-paused"
    if job.get("state") in {"completed", "error"}:
        return job["state"]
    if not enabled:
        return "paused" if has_pause_marker(job) else "disabled"
    return "enabled"


def pinned_route(job):
    """Model pin: any of provider/model/base_url. Without them the job follows the main model."""
    values = [str(job.get(key)).strip() for key in ("model", "provider", "base_url")
              if isinstance(job.get(key), str) and job.get(key).strip()]
    return " · ".join(values) or None


def chat_of(deliver):
    """`platform:chat` for an explicit `platform:chat[:thread]` target, otherwise None."""
    if not isinstance(deliver, str) or deliver.count(":") not in (1, 2) or "," in deliver:
        return None
    return ":".join(deliver.split(":")[:2])


def analyse(jobs, now, builtin_ran):
    findings, notes, seen = [], [], set()
    threaded = {chat_of(job.get("deliver")) for job in jobs
                if isinstance(job.get("deliver"), str) and job["deliver"].count(":") == 2}
    for job in jobs:
        job_id = str(job.get("id", ""))
        tag = f"{job_id or '?'} \"{job.get('name', '?')}\""
        if not job_id:
            findings.append(f"{tag}: missing id")
        elif job_id in seen:
            findings.append(f"{tag}: duplicate id")
        seen.add(job_id)
        schedule = job.get("schedule")
        if not isinstance(schedule, dict):
            findings.append(f"{tag}: schedule must be an object")
            continue
        kind = schedule.get("kind")
        if kind not in {"cron", "interval", "once"}:
            findings.append(f"{tag}: unknown schedule.kind={kind!r}")
        state = job_state(job)
        active = state == "enabled"
        if state == "half-paused":
            findings.append(f"{tag}: enabled=true with a pause marker — Hermes will not run it, "
                            "yet `cron list` shows it as scheduled; pause or resume it properly")
        if state == "error":
            findings.append(f"{tag}: state=error — {job.get('last_error') or 'no reason recorded'}")
        if job.get("no_agent") and not job.get("script"):
            findings.append(f"{tag}: no_agent=true without a script")
        streak = job.get("failure_streak")
        if active and isinstance(streak, int) and streak >= 2:
            findings.append(f"{tag}: {streak} failed runs in a row")
        status = job.get("last_status")
        if active and status == "held":
            notes.append(f"{tag}: last_status=held — runs are held until the provider quota recovers")
        if active and not builtin_ran:
            # The built-in doctor makes these checks; here they run only when it did not.
            if status in FAILED_STATUSES:
                findings.append(f"{tag}: last run — {status}: {job.get('last_error') or 'no text'}")
            if str(job.get("last_delivery_error") or "").strip():
                findings.append(f"{tag}: result not delivered — {job['last_delivery_error']}")
            nxt = parse_time(job.get("next_run_at"))
            if kind in {"cron", "interval"} and nxt is None:
                findings.append(f"{tag}: recurring job without next_run_at")
            elif nxt and (now - nxt).total_seconds() > OVERDUE_GRACE:
                findings.append(f"{tag}: next_run_at is more than 15 minutes overdue")
        if not active:
            continue
        pin = pinned_route(job)
        if pin and not job.get("no_agent"):
            notes.append(f"{tag}: pinned model ({pin}) — does not follow `hermes model` "
                         "and does not use the global fallback chain")
        deliver = job.get("deliver")
        if deliver in (None, "", "origin"):
            notes.append(f"{tag}: deliver=origin — check that the actual chat/thread is the text's audience")
        elif isinstance(deliver, str) and deliver.count(":") == 1 and chat_of(deliver) in threaded:
            notes.append(f"{tag}: deliver={deliver} without a thread although this chat uses threads — "
                         "the message goes to the general feed")
    return findings, notes


def connect(home: Path):
    database = home / "cron" / "executions.db"
    if not database.exists():
        return None, "executions.db not found"
    try:
        return sqlite3.connect(f"file:{database}?mode=ro", uri=True), None
    except sqlite3.Error as exc:
        return None, f"cannot read executions.db: {exc}"


def columns(connection, table):
    return {row[1] for row in connection.execute(f"pragma table_info({table})")}


def history(home: Path):
    """Open incidents and the latest delivery outcome per job. Older schemas simply lack them."""
    connection, error = connect(home)
    if connection is None:
        return [], {}, error
    incidents, delivery = [], {}
    try:
        if columns(connection, "cron_incidents"):
            rows = connection.execute(
                "select id, job_id, state, error, last_seen_at from cron_incidents "
                "where state in ('detected', 'alerted') order by last_seen_at desc").fetchall()
            incidents = [{"id": r[0], "job_id": r[1], "state": r[2], "error": r[3], "last_seen_at": r[4]}
                         for r in rows]
        if "delivery_outcome" in columns(connection, "executions"):
            rows = connection.execute(
                "select job_id, delivery_outcome, max(claimed_at) from executions "
                "where status = 'completed' group by job_id").fetchall()
            delivery = {r[0]: r[1] for r in rows if r[1]}
    except sqlite3.Error as exc:
        return incidents, delivery, f"cannot read executions.db: {exc}"
    finally:
        connection.close()
    return incidents, delivery, None


def runs(home: Path, job_id: str, limit: int):
    connection, error = connect(home)
    if connection is None:
        return [], error
    try:
        extra = ", delivery_outcome" if "delivery_outcome" in columns(connection, "executions") else ", null"
        rows = connection.execute(
            "select claimed_at, started_at, finished_at, status, coalesce(error, '')" + extra +
            " from executions where job_id = ? order by claimed_at desc limit ?",
            (job_id, limit),
        ).fetchall()
    except sqlite3.Error as exc:
        return [], f"cannot read executions.db: {exc}"
    finally:
        connection.close()
    return [
        {"claimed_at": r[0], "started_at": r[1], "finished_at": r[2], "status": r[3], "error": r[4],
         "delivery_outcome": r[5]}
        for r in rows
    ], None


def hermes_binary():
    explicit = os.environ.get("HERMES_BIN")
    if explicit:
        return explicit
    found = shutil.which("hermes")
    if found:
        return found
    fallback = Path("~/.local/bin/hermes").expanduser()
    return str(fallback) if fallback.exists() else None


def builtin_doctor(home: Path, enabled: bool):
    if not enabled:
        return {"ran": False, "reason": "disabled with --no-builtin"}
    binary = hermes_binary()
    if not binary:
        return {"ran": False, "reason": "hermes CLI not found (PATH, ~/.local/bin, HERMES_BIN)"}
    try:
        result = subprocess.run(
            [binary, "cron", "doctor"], env=os.environ | {"HERMES_HOME": str(home), "NO_COLOR": "1"},
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ran": False, "reason": f"could not start: {exc}"}
    output = ANSI.sub("", (result.stdout + result.stderr)).strip()
    if result.returncode not in (0, 1):
        return {"ran": False, "reason": f"exit code {result.returncode}: {output[-300:]}"}
    return {"ran": True, "exit_code": result.returncode, "output": output}


def ticker(home: Path):
    findings, notes = [], []
    beat, pid = read_stamp(home / "cron" / "ticker_heartbeat")
    success, _ = read_stamp(home / "cron" / "ticker_last_success")
    now = time.time()
    beat_age = None if beat is None else max(0, now - beat)
    success_age = None if success is None else max(0, now - success)
    if beat_age is None:
        findings.append("no ticker_heartbeat: the scheduler may not have started")
    elif beat_age > HEARTBEAT_LIMIT:
        findings.append(f"stale heartbeat: {int(beat_age)} s")
    if success_age is None:
        notes.append("no ticker_last_success: check the gateway after its first tick")
    elif success_age > SUCCESS_LIMIT:
        findings.append(f"stale successful tick: {int(success_age)} s")
    data = {"heartbeat_age_seconds": beat_age, "heartbeat_pid": pid, "success_age_seconds": success_age}
    return data, findings, notes


def report(home: Path, selected: str | None, limit: int, use_builtin: bool):
    payload, error = load_json(home / "cron" / "jobs.json")
    if error:
        return {"ok": False, "error": error}, 2
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        return {"ok": False, "error": "jobs.json must be an object with a jobs array"}, 2
    jobs = [job for job in payload["jobs"] if isinstance(job, dict)]
    now = datetime.now(timezone.utc)
    if selected:
        token = selected.casefold()
        matched = [job for job in jobs if str(job.get("id", "")).casefold() == token
                   or str(job.get("name", "")).casefold() == token]
        if not matched:
            return {"ok": False, "error": f"job \"{selected}\" not found"}, 3
        incidents, _, _ = history(home)
        details = []
        for job in matched:
            job_id = str(job.get("id", ""))
            entries, runs_error = runs(home, job_id, limit)
            details.append({"job": job, "derived_state": job_state(job), "pinned": pinned_route(job),
                            "runs": entries, "runs_error": runs_error,
                            "incidents": [item for item in incidents if item["job_id"] == job_id]})
        return {"ok": True, "jobs": details}, 0
    builtin = builtin_doctor(home, use_builtin)
    scheduler, findings, notes = ticker(home)
    job_findings, job_notes = analyse(jobs, now, builtin["ran"])
    findings += job_findings
    notes += job_notes
    if builtin["ran"] and builtin["exit_code"] == 1:
        findings.append("the built-in `hermes cron doctor` reported issues — see its output")
    elif not builtin["ran"]:
        notes.append(f"built-in doctor not run ({builtin['reason']}) — basic checks were done here")
    incidents, delivery, history_error = history(home)
    names = {str(job.get("id")): job.get("name", "?") for job in jobs}
    for item in incidents:
        findings.append(f"open incident {item['id']} — {item['job_id']} \"{names.get(item['job_id'], '?')}\": "
                        f"{str(item['error'])[:120]} (silence it: hermes cron incidents ack {item['id']})")
    for job in jobs:
        outcome = delivery.get(str(job.get("id")))
        if job_state(job) == "enabled" and outcome in BAD_DELIVERY:
            findings.append(f"{job.get('id')} \"{job.get('name', '?')}\": latest run was not delivered "
                            f"(delivery_outcome={outcome})")
    if history_error:
        notes.append(history_error)
    return {
        "ok": not findings,
        "scheduler": scheduler,
        "builtin": builtin,
        "jobs": [{"id": job.get("id"), "name": job.get("name"), "state": job_state(job),
                  "enabled": job.get("enabled", True), "schedule": job.get("schedule"),
                  "next_run_at": job.get("next_run_at"), "last_status": job.get("last_status"),
                  "deliver": job.get("deliver") or "origin", "pinned": pinned_route(job)} for job in jobs],
        "incidents": incidents,
        "findings": findings,
        "notes": notes,
    }, 1 if findings else 0


def print_human(data):
    if "error" in data:
        print(f"🔴 {data['error']}")
        return
    if "scheduler" in data:
        scheduler = data["scheduler"]
        print("== Scheduler ==")
        pid = f", PID {scheduler['heartbeat_pid']}" if scheduler["heartbeat_pid"] else ""
        print(f"  heartbeat: {describe_age(scheduler['heartbeat_age_seconds'])}{pid}")
        print(f"  successful tick: {describe_age(scheduler['success_age_seconds'])}")
        builtin = data["builtin"]
        print("\n== Built-in hermes cron doctor ==")
        if builtin["ran"]:
            for line in builtin["output"].splitlines() or ["(empty output)"]:
                print(f"  {line}")
        else:
            print(f"  not run: {builtin['reason']}")
        print("\n== Jobs ==")
        for job in data["jobs"]:
            print(f"  {job['state']:<11} {job['id'] or '?':<14} {job['name'] or '?'} · next: {job['next_run_at'] or '—'}")
        print("\n== Findings ==")
        for item in data["findings"] or ["none"]:
            print(f"  {'🔴' if item != 'none' else '✅'} {item}")
        if data["notes"]:
            print("\n== Notes ==")
            for item in data["notes"]:
                print(f"  ℹ️ {item}")
        return
    for detail in data["jobs"]:
        job = detail["job"]
        print(f"== {job.get('id')} \"{job.get('name')}\" ==")
        print(f"  state: {detail['derived_state']} (enabled={job.get('enabled', True)}, state={job.get('state')})")
        print(f"  schedule: {job.get('schedule_display') or job.get('schedule')}")
        print(f"  delivery: {job.get('deliver') or 'origin'}"
              + (f" · failures: {job['failure_deliver']}" if job.get("failure_deliver") else ""))
        print(f"  model: {detail['pinned'] or 'main model at fire time'}")
        modes = [name for name, on in (("no_agent", job.get("no_agent")), ("script", job.get("script")),
                                       ("monitor", job.get("monitor_script") or job.get("monitor_url")),
                                       ("context_from", job.get("context_from"))) if on]
        print(f"  mode: {', '.join(modes) or 'LLM'}")
        print(f"  last status: {job.get('last_status') or '—'}"
              + (f" — {job['last_error']}" if job.get("last_error") else ""))
        for entry in detail["runs"]:
            outcome = f" · delivery: {entry['delivery_outcome']}" if entry["delivery_outcome"] else ""
            print(f"  {entry['claimed_at']} {entry['status']}{outcome} {entry['error']}".rstrip())
        for item in detail["incidents"]:
            print(f"  🔴 incident {item['id']} ({item['state']}): {str(item['error'])[:120]}")
        if detail["runs_error"]:
            print(f"  ℹ️ {detail['runs_error']}")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Exit code 2 means "unreadable runtime"; argument errors are 3, as the README promises.
        self.print_usage(sys.stderr)
        self.exit(3, f"{self.prog}: error: {message}\n")


def main():
    parser = Parser()
    parser.add_argument("--job", help="job id or exact name")
    parser.add_argument("--runs", type=int, default=10, help="number of runs to show with --job")
    parser.add_argument("--json", action="store_true", help="stable JSON for automation")
    parser.add_argument("--no-builtin", action="store_true", help="do not run `hermes cron doctor`")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs must be positive")
    data, code = report(hermes_home(), args.job, args.runs, not args.no_builtin)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print_human(data)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
