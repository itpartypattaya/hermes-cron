#!/usr/bin/env python3
"""Read-only health check for a standard Hermes cron installation.

Complements the built-in `hermes cron doctor` instead of duplicating it: runs it when the CLI is
available, then adds what it does not look at — ticker stamps, disabled and paused jobs, duplicate
ids, malformed entries, half-paused records, consecutive failures, the delivery outcome of each job's
latest completed run, open failure incidents, the same error across several jobs, pinned models,
thread-less delivery into chats that use threads, a cron store that cannot be written (Hermes 0.21.6+
then skips due jobs) and the sticky `last_failure` of a job that has recovered since. Without the CLI (or with --no-builtin) the basic
checks the built-in would have made — failed last run, undelivered result, overdue next_run_at — run
here.

The same error in three or more jobs within a day is reported once, as one fault of the scheduler or
its host: per-job fields hide it. `last_status` and `failure_streak` reset on the next good run (only
`last_failure`, added in 0.21.6, keeps the last failure), and a
dispatch failure may not open an incident at all — on Hermes 0.21.3 every cron run failed for days
with "Restart-safe cron worker dispatch failed … exited before ownership acknowledgement" while the
gateway user had no lingering systemd session, and no job looked broken for long.

The doctor itself never ticks, executes or changes a job. The built-in doctor is Hermes code: its
job loader, like every scheduler tick, may repair malformed entries in jobs.json; --no-builtin gives
a strictly read-only pass.

No custom sync or deployment layer is assumed. Exit codes: 0 healthy, 1 findings, 2 unreadable
runtime state, 3 invalid arguments or an unknown job. Use --json for automation.
"""
from __future__ import annotations

import argparse
import json
import math
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
FUTURE_SKEW = 300            # a stamp further ahead than this is a clock problem or corruption
OVERDUE_GRACE = 15 * 60      # same grace as `hermes cron doctor` and `cron status`
FAILED_STATUSES = {"error", "blocked_config", "interrupted"}
BAD_DELIVERY = {"failed", "not_configured"}
INCIDENT_COLUMNS = {"id", "job_id", "state", "error", "last_seen_at"}
EXECUTION_COLUMNS = {"job_id", "status", "claimed_at"}
OPTIONAL_RUN_COLUMNS = ("started_at", "finished_at", "error", "delivery_outcome")
SHARED_WINDOW = 24 * 3600    # how far back failed runs are grouped by error
SHARED_MIN_JOBS = 3          # this many different jobs with one error = one scheduler or host fault
FAILED_RUN_STATUSES = ("failed", "unknown")
RECOVERED_STATUSES = {"ok", "delivery_queued"}
LOW_FREE_BYTES = 100 << 20   # `hermes doctor` warns below this before store writes start failing
EPOCH = datetime(1970, 1, 1, tzinfo=timezone.utc)
ANSI = re.compile(r"\x1b\[[0-9;]*m")
# Run-specific details that would split one fault into many: timestamps, hex ids, measured durations
# (Hermes 0.21.6 masks them in incident signatures too: "idle for 603s"), PIDs and ports.
ERROR_NOISE = (
    (re.compile(r"\d{4}-\d{2}-\d{2}[t ][\d:.,]+(?:[+-]\d{2}:?\d{2}|z)?"), "<time>"),
    (re.compile(r"\b[0-9a-f]{8,}\b"), "<id>"),
    (re.compile(r"\b\d+(?:\.\d+)?\s*(?:ms|s|secs?|seconds?|m|mins?|minutes?|h|hrs?|hours?)\b"), "<duration>"),
    (re.compile(r"\b\d{5,}\b"), "<n>"),
)
# A gateway restart interrupts every running job with the same text; that is an intended event,
# not a broken scheduler (a crash loop shows up in the ticker stamps instead).
RESTART_ERRORS = (
    "interrupted by shutdown",
    "fire claim ownership lost",
    "scheduler restarted after this execution's owner exited",
)


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or os.environ.get("HERMES_DIR") or "~/.hermes").expanduser()


def load_json(path: Path):
    try:
        with path.open(encoding="utf-8-sig") as stream:
            return json.load(stream), None
    except FileNotFoundError:
        return None, f"missing {path}"
    except (OSError, ValueError) as exc:  # ValueError covers JSONDecodeError and UnicodeDecodeError
        return None, f"cannot read {path}: {exc}"


def text(value):
    """The value when it is a string, otherwise None — guards set lookups against lists and dicts."""
    return value if isinstance(value, str) else None


def parse_time(value):
    """Aware datetime, or None. A timestamp without a zone is ambiguous and returns None."""
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else None


def is_naive(value) -> bool:
    if not isinstance(value, str) or not value:
        return False
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).tzinfo is None
    except ValueError:
        return False


def instant(value):
    """Sort key that orders mixed UTC offsets by the real moment; unparsable values sort first."""
    return parse_time(value) or EPOCH


def read_stamp(path: Path):
    """(epoch, pid, problem). Hermes 0.21.5 writes `<epoch> <pid>`, older versions `<epoch>`."""
    try:
        fields = path.read_text(encoding="utf-8").split()
    except FileNotFoundError:
        return None, None, None
    except (OSError, ValueError) as exc:
        return None, None, f"cannot read {path.name}: {exc}"
    try:
        epoch = float(fields[0])
    except (ValueError, IndexError):
        return None, None, f"{path.name} does not hold a timestamp"
    if not math.isfinite(epoch) or epoch <= 0:
        return None, None, f"{path.name} holds an invalid timestamp ({fields[0]})"
    pid = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
    return epoch, pid, None


def describe_age(seconds):
    if seconds is None:
        return "no stamp"
    if seconds < 90:
        return f"{int(seconds)} s ago"
    if seconds < 5400:
        return f"{int(seconds // 60)} min ago"
    return f"{int(seconds // 3600)} h ago"


def job_id_of(job) -> str:
    value = job.get("id")
    if isinstance(value, bool) or not isinstance(value, (str, int)):
        return ""
    return str(value).strip()


def tag(job) -> str:
    name = job.get("name")
    return f"{job_id_of(job) or '?'} \"{name if isinstance(name, str) else '?'}\""


def has_pause_marker(job) -> bool:
    return text(job.get("state")) == "paused" or bool(job.get("paused_at"))


def job_state(job):
    enabled = job.get("enabled", True)
    if enabled and has_pause_marker(job):
        return "half-paused"
    state = text(job.get("state"))
    if state in {"completed", "error"}:
        return state
    if not enabled:
        return "paused" if has_pause_marker(job) else "disabled"
    return "enabled"


STATE_ORDER = ("enabled", "half-paused", "paused", "disabled", "completed", "error")


def summarize(jobs):
    """Job counts by derived state, so "how many jobs are active" needs no extra parsing."""
    counts = {"total": len(jobs)}
    for state in STATE_ORDER:
        counts[state] = sum(1 for job in jobs if job_state(job) == state)
    return counts


def pinned_route(job):
    """Model pin: any of provider/model/base_url. Without them the job follows the main model."""
    values = [job[key].strip() for key in ("model", "provider", "base_url")
              if isinstance(job.get(key), str) and job[key].strip()]
    return " · ".join(values) or None


def recovered_failure(job):
    """`<at>: <detail>` of the sticky `last_failure` (Hermes 0.21.6+) when the job has run fine since."""
    failure = job.get("last_failure")
    if not isinstance(failure, dict) or not str(failure.get("detail") or "").strip():
        return None
    if text(job.get("last_status")) not in RECOVERED_STATUSES:
        return None  # still failing: last_status and last_error already say so
    return f"{failure.get('at') or '?'}: {str(failure['detail'])[:160]}"


def store_findings(cron_dir: Path, writable: bool, free_bytes):
    """Findings for the cron store. Hermes 0.21.6+ skips a due job whose run it cannot record."""
    if not writable:
        return [f"cron store {cron_dir} is not writable by this user — Hermes skips due jobs it cannot "
                "record; fix permissions or the mount, then each due job fires once"]
    if free_bytes is not None and free_bytes < LOW_FREE_BYTES:
        return [f"cron store {cron_dir}: only {free_bytes // (1 << 20)} MB free — when the disk fills up, "
                "Hermes skips due jobs; free disk space"]
    return []


def store_health(home: Path):
    """Read-only probe: permission bits and free space. Never creates a file in the store."""
    cron_dir = home / "cron"
    try:
        free = shutil.disk_usage(cron_dir).free
    except OSError:
        free = None
    return store_findings(cron_dir, os.access(cron_dir, os.W_OK | os.X_OK), free)


def chat_of(deliver):
    """`platform:chat` for an explicit `platform:chat[:thread]` target, otherwise None."""
    if not isinstance(deliver, str) or deliver.count(":") not in (1, 2) or "," in deliver:
        return None
    return ":".join(deliver.split(":")[:2])


def threaded_chats(jobs):
    return {chat_of(job.get("deliver")) for job in jobs
            if isinstance(job.get("deliver"), str) and job["deliver"].count(":") == 2}


def analyse_job(job, now, builtin_ran, threaded):
    findings, notes = [], []
    label = tag(job)
    if not job_id_of(job):
        findings.append(f"{label}: missing or invalid id")
    schedule = job.get("schedule")
    if not isinstance(schedule, dict):
        findings.append(f"{label}: schedule must be an object")
        return findings, notes
    kind = text(schedule.get("kind"))
    if kind not in {"cron", "interval", "once"}:
        findings.append(f"{label}: unknown schedule.kind={schedule.get('kind')!r}")
    state = job_state(job)
    active = state == "enabled"
    if state == "half-paused":
        findings.append(f"{label}: enabled=true with a pause marker — Hermes will not run it, "
                        "yet `cron list` shows it as scheduled; pause or resume it properly")
    if state == "error":
        findings.append(f"{label}: state=error — {job.get('last_error') or 'no reason recorded'}")
    if job.get("no_agent") and not job.get("script"):
        findings.append(f"{label}: no_agent=true without a script")
    failure = recovered_failure(job)
    if failure:
        notes.append(f"{label}: last failure at {failure} — recovered since; read the run history")
    streak = job.get("failure_streak")
    if active and isinstance(streak, int) and not isinstance(streak, bool) and streak >= 2:
        findings.append(f"{label}: {streak} failed runs in a row")
    status = text(job.get("last_status"))
    if active and status == "held":
        notes.append(f"{label}: last_status=held — runs are held until the provider quota recovers")
    if active and not builtin_ran:
        # The built-in doctor makes these checks; here they run only when it did not.
        if status in FAILED_STATUSES:
            findings.append(f"{label}: last run — {status}: {job.get('last_error') or 'no text'}")
        if str(job.get("last_delivery_error") or "").strip():
            findings.append(f"{label}: result not delivered — {job['last_delivery_error']}")
        raw_next = job.get("next_run_at")
        nxt = parse_time(raw_next)
        if is_naive(raw_next):
            notes.append(f"{label}: next_run_at has no timezone — overdue check skipped")
        elif kind in {"cron", "interval"} and nxt is None:
            findings.append(f"{label}: recurring job without next_run_at")
        elif nxt and (now - nxt).total_seconds() > OVERDUE_GRACE:
            findings.append(f"{label}: next_run_at is more than 15 minutes overdue")
    if not active:
        return findings, notes
    pin = pinned_route(job)
    if pin and not job.get("no_agent"):
        notes.append(f"{label}: pinned model ({pin}) — does not follow `hermes model` "
                     "and does not use the global fallback chain")
    deliver = job.get("deliver")
    if deliver in (None, "", "origin"):
        notes.append(f"{label}: deliver=origin — check that the actual chat/thread is the text's audience")
    elif isinstance(deliver, str) and deliver.count(":") == 1 and chat_of(deliver) in threaded:
        notes.append(f"{label}: deliver={deliver} without a thread although this chat uses threads — "
                     "the message goes to the general feed")
    return findings, notes


def analyse(jobs, now, builtin_ran):
    findings, notes, seen = [], [], set()
    threaded = threaded_chats(jobs)
    for job in jobs:
        job_id = job_id_of(job)
        if job_id and job_id in seen:
            findings.append(f"{tag(job)}: duplicate id")
        seen.add(job_id)
        job_findings, job_notes = analyse_job(job, now, builtin_ran, threaded)
        findings += job_findings
        notes += job_notes
    return findings, notes


def connect(home: Path):
    database = home / "cron" / "executions.db"
    if not database.exists():
        return None, "executions.db not found"
    try:
        # as_uri() percent-encodes '#', '?', '%' and non-ASCII characters in the path.
        return sqlite3.connect(database.resolve().as_uri() + "?mode=ro", uri=True), None
    except (sqlite3.Error, ValueError) as exc:
        return None, f"cannot open executions.db: {exc}"


def columns(connection, table):
    return {row[1] for row in connection.execute(f"pragma table_info({table})")}


def history(home: Path):
    """Open incidents and each job's latest completed run as (outcome, claimed_at).

    Returns (incidents, delivery, problems, remarks): problems mean the history could not be read and
    the check is incomplete; remarks are expected gaps such as an older schema. Each table is read on
    its own, so a damaged incidents table does not hide delivery failures and vice versa.
    """
    connection, error = connect(home)
    if connection is None:
        return [], {}, [], [error]
    incidents, delivery, problems, remarks = [], {}, [], []
    try:
        try:
            available = columns(connection, "cron_incidents")
            if available and INCIDENT_COLUMNS <= available:
                rows = connection.execute(
                    "select id, job_id, state, error, last_seen_at from cron_incidents "
                    "where state in ('detected', 'alerted')").fetchall()
                incidents = sorted(
                    ({"id": r[0], "job_id": str(r[1]), "state": r[2], "error": r[3], "last_seen_at": r[4]}
                     for r in rows),
                    key=lambda item: instant(item["last_seen_at"]), reverse=True)
            elif available:
                remarks.append("cron_incidents has an unexpected schema — incidents not checked")
        except sqlite3.Error as exc:
            problems.append(f"cannot read cron_incidents: {exc}")
        try:
            available = columns(connection, "executions")
            if available and EXECUTION_COLUMNS | {"delivery_outcome"} <= available:
                best = {}
                for job_id, outcome, claimed, rowid in connection.execute(
                        "select job_id, delivery_outcome, claimed_at, rowid from executions "
                        "where status = 'completed'"):
                    key = (instant(claimed), rowid)
                    if job_id not in best or key > best[job_id][0]:
                        best[job_id] = (key, outcome, claimed)
                delivery = {str(job_id): (outcome, claimed) for job_id, (_, outcome, claimed) in best.items()}
            elif available and EXECUTION_COLUMNS <= available:
                remarks.append("executions has no delivery_outcome column — delivery not checked")
            elif available:
                remarks.append("executions has an unexpected schema — delivery not checked")
        except sqlite3.Error as exc:
            problems.append(f"cannot read executions: {exc}")
    finally:
        connection.close()
    return incidents, delivery, problems, remarks


def error_key(error) -> str:
    """The error with run-specific details masked, so one fault in many jobs groups into one key."""
    value = " ".join(str(error or "").lower().split())
    for pattern, mask in ERROR_NOISE:
        value = pattern.sub(mask, value)
    return value[:200]


def shared_failures(home: Path, now: datetime):
    """Errors that failed SHARED_MIN_JOBS or more different jobs within SHARED_WINDOW.

    Returns (groups, problems, remarks); each group is a dict with the first error text seen, the
    sorted job ids, the number of failed runs and the first and last instant. A missing database is
    left to history(), which already reports it.
    """
    connection, _ = connect(home)
    if connection is None:
        return [], [], []
    try:
        available = columns(connection, "executions")
        if not available:
            return [], [], []
        if not {"job_id", "status", "claimed_at", "error"} <= available:
            return [], [], ["executions has no error column — shared failures not checked"]
        finished = "finished_at" if "finished_at" in available else "null"
        rows = connection.execute(
            f"select job_id, error, claimed_at, {finished} from executions "
            "where status in (?, ?) and coalesce(error, '') != ''", FAILED_RUN_STATUSES).fetchall()
    except sqlite3.Error as exc:
        return [], [f"cannot read executions for shared failures: {exc}"], []
    finally:
        connection.close()
    since = now.timestamp() - SHARED_WINDOW
    groups = {}
    for job_id, error, claimed, finished_at in rows:
        moment = parse_time(finished_at) or parse_time(claimed)
        key = error_key(error)
        if moment is None or not since <= moment.timestamp() <= now.timestamp() \
                or key.startswith(RESTART_ERRORS):
            continue
        group = groups.setdefault(key, {
            "error": str(error), "jobs": set(), "runs": 0, "first": moment, "last": moment})
        group["jobs"].add(str(job_id))
        group["runs"] += 1
        group["first"], group["last"] = min(group["first"], moment), max(group["last"], moment)
    found = [dict(group, jobs=sorted(group["jobs"]), first=group["first"].isoformat(),
                  last=group["last"].isoformat())
             for group in groups.values() if len(group["jobs"]) >= SHARED_MIN_JOBS]
    return sorted(found, key=lambda g: (-len(g["jobs"]), -g["runs"])), [], []


def shared_finding(group, names) -> str:
    listed = ", ".join(f"{job_id} \"{names.get(job_id, '?')}\"" for job_id in group["jobs"][:5])
    more = f" and {len(group['jobs']) - 5} more" if len(group["jobs"]) > 5 else ""
    return (f"the same error failed {len(group['jobs'])} jobs in the last 24 h ({group['runs']} runs, "
            f"{group['first']} … {group['last']}) — the scheduler or its host is broken, not the jobs; "
            f"fix that first: {group['error'][:200]} · jobs: {listed}{more}")


def runs(home: Path, job_id: str, limit: int):
    connection, error = connect(home)
    if connection is None:
        return [], error
    try:
        available = columns(connection, "executions")
        if not EXECUTION_COLUMNS <= available:
            return [], "executions has an unexpected schema"
        optional = ", ".join(name if name in available else "null" for name in OPTIONAL_RUN_COLUMNS)
        rows = connection.execute(
            f"select claimed_at, status, {optional}, rowid from executions where job_id = ?",
            (job_id,)).fetchall()
    except sqlite3.Error as exc:
        return [], f"cannot read executions: {exc}"
    finally:
        connection.close()
    rows.sort(key=lambda r: (instant(r[0]), r[-1]), reverse=True)
    return [
        {"claimed_at": r[0], "status": r[1], "started_at": r[2], "finished_at": r[3],
         "error": r[4] or "", "delivery_outcome": r[5]}
        for r in rows[:limit]
    ], None


def history_findings(job, incidents, delivery, names):
    """Findings for one job from the run history: open incidents and an undelivered latest run."""
    findings, job_id = [], job_id_of(job)
    for item in incidents:
        if item["job_id"] == job_id:
            findings.append(f"open incident {item['id']} — {job_id} \"{names.get(job_id, '?')}\": "
                            f"{str(item['error'])[:120]} (silence it: hermes cron incidents ack {item['id']})")
    outcome, claimed = delivery.get(job_id, (None, None))
    if job_state(job) == "enabled" and outcome in BAD_DELIVERY:
        findings.append(f"{tag(job)}: latest completed run ({claimed}) was not delivered "
                        f"(delivery_outcome={outcome})")
    return findings


def hermes_binary():
    explicit = os.environ.get("HERMES_BIN")
    if explicit:
        return explicit
    found = shutil.which("hermes")
    if found:
        return found
    fallback = Path("~/.local/bin/hermes").expanduser()
    return str(fallback) if fallback.exists() else None


def builtin_doctor(enabled: bool):
    if not enabled:
        return {"ran": False, "reason": "disabled with --no-builtin"}
    binary = hermes_binary()
    if not binary:
        return {"ran": False, "reason": "hermes CLI not found (PATH, ~/.local/bin, HERMES_BIN)"}
    try:
        result = subprocess.run(
            [binary, "cron", "doctor"], capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ran": False, "reason": f"could not start: {exc}"}
    output = ANSI.sub("", (result.stdout + result.stderr)).strip()
    if result.returncode not in (0, 1):
        return {"ran": False, "reason": f"exit code {result.returncode}: {output[-300:]}"}
    return {"ran": True, "exit_code": result.returncode, "output": output}


def stamp_age(name, epoch, problem, now):
    """(age, finding) for one ticker stamp; a stamp far in the future is reported, never shown as fresh."""
    if problem:
        return None, problem
    if epoch is None:
        return None, None
    age = now - epoch
    if age < -FUTURE_SKEW:
        return None, f"{name} is {int(-age)} s in the future — clock skew or a corrupt stamp"
    return max(0.0, age), None


def ticker(home: Path):
    findings, notes = [], []
    now = time.time()
    beat, pid, beat_problem = read_stamp(home / "cron" / "ticker_heartbeat")
    success, _, success_problem = read_stamp(home / "cron" / "ticker_last_success")
    beat_age, beat_finding = stamp_age("ticker_heartbeat", beat, beat_problem, now)
    success_age, success_finding = stamp_age("ticker_last_success", success, success_problem, now)
    if beat_finding:
        findings.append(beat_finding)
    elif beat_age is None:
        findings.append("no ticker_heartbeat: the scheduler may not have started")
    elif beat_age > HEARTBEAT_LIMIT:
        findings.append(f"stale heartbeat: {int(beat_age)} s")
    if success_finding:
        findings.append(success_finding)
    elif success_age is None:
        notes.append("no ticker_last_success: check the gateway after its first tick")
    elif success_age > SUCCESS_LIMIT:
        findings.append(f"stale successful tick: {int(success_age)} s")
    data = {"heartbeat_age_seconds": beat_age, "heartbeat_pid": pid, "success_age_seconds": success_age}
    return data, findings, notes


def load_jobs(home: Path):
    """(jobs, structural findings, error). Non-object entries are reported, not silently dropped."""
    payload, error = load_json(home / "cron" / "jobs.json")
    if error:
        return None, [], error
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        return None, [], "jobs.json must be an object with a jobs array"
    raw = payload["jobs"]
    junk = [f"jobs.json entry #{index} is not an object ({type(item).__name__}) — Hermes skips it"
            for index, item in enumerate(raw) if not isinstance(item, dict)]
    return [item for item in raw if isinstance(item, dict)], junk, None


def report(home: Path, selected: str | None, limit: int, use_builtin: bool):
    jobs, structure, error = load_jobs(home)
    if error:
        return {"ok": False, "error": error}, 2
    now = datetime.now(timezone.utc)
    names = {job_id_of(job): job.get("name", "?") for job in jobs}
    if selected:
        token = selected.casefold()
        matched = [job for job in jobs if job_id_of(job).casefold() == token
                   or text(job.get("name")) is not None and job["name"].casefold() == token]
        if not matched:
            return {"ok": False, "error": f"job \"{selected}\" not found"}, 3
        incidents, delivery, problems, remarks = history(home)
        threaded = threaded_chats(jobs)
        details, any_findings = [], bool(problems)
        for job in matched:
            job_id = job_id_of(job)
            findings, notes = analyse_job(job, now, False, threaded)
            if sum(1 for other in jobs if job_id and job_id_of(other) == job_id) > 1:
                findings.append(f"{tag(job)}: duplicate id")
            findings += history_findings(job, incidents, delivery, names)
            entries, runs_error = runs(home, job_id, limit)
            if runs_error:
                notes.append(runs_error)
            any_findings = any_findings or bool(findings)
            details.append({"job": job, "derived_state": job_state(job), "pinned": pinned_route(job),
                            "findings": findings, "notes": notes, "runs": entries,
                            "incidents": [item for item in incidents if item["job_id"] == job_id]})
        return {"ok": not any_findings, "jobs": details, "history_problems": problems,
                "history_notes": remarks}, 1 if any_findings else 0
    builtin = builtin_doctor(use_builtin)
    scheduler, findings, notes = ticker(home)
    findings += store_health(home)
    shared, shared_problems, shared_remarks = shared_failures(home, now)
    findings += [shared_finding(group, names) for group in shared] + shared_problems
    notes += shared_remarks
    findings += structure
    job_findings, job_notes = analyse(jobs, now, builtin["ran"])
    findings += job_findings
    notes += job_notes
    if builtin["ran"] and builtin["exit_code"] == 1:
        findings.append("the built-in `hermes cron doctor` reported issues — see its output")
    elif not builtin["ran"]:
        notes.append(f"built-in doctor not run ({builtin['reason']}) — basic checks were done here")
    incidents, delivery, problems, remarks = history(home)
    for job in jobs:
        findings += history_findings(job, incidents, delivery, names)
    findings += problems
    notes += remarks
    return {
        "ok": not findings,
        "scheduler": scheduler,
        "builtin": builtin,
        "summary": summarize(jobs),
        "jobs": [{"id": job_id_of(job) or None, "name": job.get("name"), "state": job_state(job),
                  "enabled": job.get("enabled", True), "schedule": job.get("schedule"),
                  "next_run_at": job.get("next_run_at"), "last_status": job.get("last_status"),
                  "deliver": job.get("deliver") or "origin", "pinned": pinned_route(job),
                  "last_failure": job.get("last_failure")} for job in jobs],
        "incidents": incidents,
        "shared_failures": shared,
        "findings": findings,
        "notes": notes,
    }, 1 if findings else 0


def print_list(title, items, mark):
    if items:
        print(f"\n== {title} ==")
        for item in items:
            print(f"  {mark} {item}")


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
        summary = data["summary"]
        print("\n== Jobs ==")
        print("  " + " · ".join([f"{summary['total']} total"]
                                + [f"{summary[state]} {state}" for state in STATE_ORDER if summary[state]]))
        for job in data["jobs"]:
            print(f"  {job['state']:<11} {job['id'] or '?':<14} {job['name'] or '?'} · next: {job['next_run_at'] or '—'}")
        print("\n== Findings ==")
        for item in data["findings"] or ["none"]:
            print(f"  {'🔴' if item != 'none' else '✅'} {item}")
        print_list("Notes", data["notes"], "ℹ️")
        return
    for detail in data["jobs"]:
        job = detail["job"]
        print(f"== {job.get('id')} \"{job.get('name')}\" ==")
        print(f"  state: {detail['derived_state']} (enabled={job.get('enabled', True)}, state={job.get('state')})")
        print(f"  schedule: {job.get('schedule_display') or job.get('schedule')}")
        print(f"  next run: {job.get('next_run_at') or '—'} · last run: {job.get('last_run_at') or '—'}")
        print(f"  delivery: {job.get('deliver') or 'origin'}"
              + (f" · failures: {job['failure_deliver']}" if job.get("failure_deliver") else ""))
        print(f"  model: {detail['pinned'] or 'main model at fire time'}")
        modes = [name for name, on in (("no_agent", job.get("no_agent")), ("script", job.get("script")),
                                       ("monitor", job.get("monitor_script") or job.get("monitor_url")),
                                       ("context_from", job.get("context_from"))) if on]
        print(f"  mode: {', '.join(modes) or 'LLM'}")
        print(f"  last status: {job.get('last_status') or '—'}"
              + (f" — {job['last_error']}" if job.get("last_error") else ""))
        failure = job.get("last_failure")
        if isinstance(failure, dict) and failure.get("detail"):
            print(f"  last failure: {failure.get('at') or '?'} — {str(failure['detail'])[:160]}")
        for entry in detail["runs"]:
            outcome = f" · delivery: {entry['delivery_outcome']}" if entry["delivery_outcome"] else ""
            print(f"  {entry['claimed_at']} {entry['status']}{outcome} {entry['error']}".rstrip())
        for item in detail["findings"] or ["no findings"]:
            print(f"  {'🔴' if detail['findings'] else '✅'} {item}")
        for item in detail["notes"]:
            print(f"  ℹ️ {item}")
    for item in data.get("history_problems", []):
        print(f"🔴 {item}")
    for item in data.get("history_notes", []):
        print(f"ℹ️ {item}")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Exit code 2 means "unreadable runtime"; argument errors are 3, as the README promises.
        if "--json" in sys.argv[1:]:
            print(json.dumps({"ok": False, "error": f"invalid arguments: {message}"}, ensure_ascii=False))
            self.exit(3)
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
    try:
        code = main()
        sys.stdout.flush()
    except BrokenPipeError:
        # The reader went away (for example `| head`); stay quiet instead of printing a traceback.
        os.dup2(os.open(os.devnull, os.O_WRONLY), sys.stdout.fileno())
        code = 1
    raise SystemExit(code)
