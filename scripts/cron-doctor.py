#!/usr/bin/env python3
"""Read-only health check for a standard Hermes cron installation.

No custom sync or deployment layer is assumed. Exit codes: 0 healthy, 1 findings,
2 unreadable runtime state, 3 invalid arguments. Use --json for automation.
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path


for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def hermes_home() -> Path:
    return Path(os.environ.get("HERMES_HOME") or os.environ.get("HERMES_DIR") or "~/.hermes").expanduser()


def load_json(path: Path):
    try:
        with path.open(encoding="utf-8-sig") as stream:
            return json.load(stream), None
    except FileNotFoundError:
        return None, f"нет {path}"
    except (OSError, json.JSONDecodeError) as exc:
        return None, f"не прочитать {path}: {exc}"


def parse_time(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.astimezone()


def age_seconds(path: Path):
    try:
        return max(0, time.time() - float(path.read_text(encoding="utf-8").strip()))
    except (OSError, ValueError):
        return None


def describe_age(seconds):
    if seconds is None:
        return "нет отметки"
    if seconds < 90:
        return f"{int(seconds)} с назад"
    if seconds < 5400:
        return f"{int(seconds // 60)} м назад"
    return f"{int(seconds // 3600)} ч назад"


def job_state(job):
    enabled = job.get("enabled", True)
    state = job.get("state")
    if enabled and state == "paused":
        return "corrupt"
    if not enabled and state == "paused":
        return "paused"
    if state == "error":
        return "error"
    if not enabled:
        return "disabled"
    return "enabled"


def runs(home: Path, job_id: str, limit: int):
    database = home / "cron" / "executions.db"
    if not database.exists():
        return [], "executions.db не найден"
    try:
        connection = sqlite3.connect(f"file:{database}?mode=ro", uri=True)
        rows = connection.execute(
            "select claimed_at, started_at, finished_at, status, coalesce(error, '') "
            "from executions where job_id = ? order by claimed_at desc limit ?",
            (job_id, limit),
        ).fetchall()
        connection.close()
    except sqlite3.Error as exc:
        return [], f"не прочитать executions.db: {exc}"
    return [
        {"claimed_at": row[0], "started_at": row[1], "finished_at": row[2], "status": row[3], "error": row[4]}
        for row in rows
    ], None


def analyse(jobs, now):
    findings, notes, seen = [], [], set()
    for job in jobs:
        job_id = str(job.get("id", ""))
        name = str(job.get("name", "?"))
        tag = f"{job_id or '?'} «{name}»"
        if not job_id:
            findings.append(f"{tag}: отсутствует id")
        elif job_id in seen:
            findings.append(f"{tag}: повторяющийся id")
        seen.add(job_id)
        if not isinstance(job.get("schedule"), dict):
            findings.append(f"{tag}: schedule должен быть объектом")
            continue
        state = job_state(job)
        if state == "corrupt":
            findings.append(f"{tag}: enabled=true при state=paused")
        if state == "error":
            findings.append(f"{tag}: state=error — {job.get('last_error') or 'причина не записана'}")
        if job.get("no_agent") and not job.get("script"):
            findings.append(f"{tag}: no_agent=true без script")
        if job.get("script") and not Path(str(job["script"])).name:
            findings.append(f"{tag}: пустое имя script")
        if job.get("last_status") == "error" and job.get("enabled", True):
            findings.append(f"{tag}: последний запуск завершился ошибкой")
        schedule = job["schedule"]
        kind = schedule.get("kind")
        if kind not in {"cron", "interval", "once"}:
            findings.append(f"{tag}: неизвестный schedule.kind={kind!r}")
        nxt = parse_time(job.get("next_run_at"))
        if job.get("enabled", True) and state not in {"paused", "error"}:
            if kind in {"cron", "interval"} and nxt is None:
                findings.append(f"{tag}: повторяющаяся задача без next_run_at")
            elif nxt and (now - nxt).total_seconds() > 3600:
                findings.append(f"{tag}: next_run_at просрочен более чем на час")
        deliver = job.get("deliver")
        if deliver == "origin":
            notes.append(f"{tag}: deliver=origin — сверить фактический chat/thread с адресатом текста")
        if isinstance(deliver, str) and deliver.count(":") == 1:
            notes.append(f"{tag}: явная delivery без thread/topic; убедись, что это намеренно")
    return findings, notes


def report(home: Path, selected: str | None, limit: int):
    payload, error = load_json(home / "cron" / "jobs.json")
    if error:
        return {"ok": False, "error": error}, 2
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        return {"ok": False, "error": "jobs.json должен содержать объект с массивом jobs"}, 2
    jobs = payload["jobs"]
    now = datetime.now(timezone.utc)
    if selected:
        token = selected.casefold()
        matched = [job for job in jobs if str(job.get("id", "")).casefold() == token or str(job.get("name", "")).casefold() == token]
        if not matched:
            return {"ok": False, "error": f"задача «{selected}» не найдена"}, 3
        details = []
        for job in matched:
            history, history_error = runs(home, str(job.get("id", "")), limit)
            details.append({"job": job, "derived_state": job_state(job), "runs": history, "runs_error": history_error})
        return {"ok": True, "jobs": details}, 0
    heartbeat = age_seconds(home / "cron" / "ticker_heartbeat")
    success = age_seconds(home / "cron" / "ticker_last_success")
    findings, notes = analyse(jobs, now)
    if heartbeat is None:
        findings.append("нет ticker_heartbeat: scheduler мог не стартовать")
    elif heartbeat > 180:
        findings.append(f"heartbeat устарел: {int(heartbeat)} с")
    if success is None:
        notes.append("нет ticker_last_success: проверь gateway после первого тика")
    elif success > 600:
        findings.append(f"последний успешный тик устарел: {int(success)} с")
    return {
        "ok": not findings,
        "scheduler": {"heartbeat_age_seconds": heartbeat, "success_age_seconds": success},
        "jobs": [{"id": job.get("id"), "name": job.get("name"), "state": job_state(job), "enabled": job.get("enabled", True), "schedule": job.get("schedule"), "next_run_at": job.get("next_run_at"), "deliver": job.get("deliver", "origin")} for job in jobs],
        "findings": findings,
        "notes": notes,
    }, 1 if findings else 0


def print_human(data):
    if "error" in data:
        print(f"🔴 {data['error']}")
        return
    if "scheduler" in data:
        scheduler = data["scheduler"]
        print("== Планировщик ==")
        print(f"  heartbeat: {describe_age(scheduler['heartbeat_age_seconds'])}")
        print(f"  успешный тик: {describe_age(scheduler['success_age_seconds'])}")
        print("\n== Задачи ==")
        for job in data["jobs"]:
            print(f"  {job['state']:<9} {job['id'] or '?':<14} {job['name'] or '?'} · след: {job['next_run_at'] or '—'}")
        print("\n== Находки ==")
        for item in data["findings"] or ["нет"]:
            print(f"  {'🔴' if item != 'нет' else '✅'} {item}")
        if data["notes"]:
            print("\n== К сведению ==")
            for item in data["notes"]:
                print(f"  ℹ️ {item}")
        return
    for detail in data["jobs"]:
        job = detail["job"]
        print(f"== {job.get('id')} «{job.get('name')}» ==")
        print(f"  состояние: {detail['derived_state']} (enabled={job.get('enabled', True)}, state={job.get('state')})")
        print(f"  schedule: {job.get('schedule')}")
        print(f"  доставка: {job.get('deliver', 'origin')}")
        for entry in detail["runs"]:
            print(f"  {entry['claimed_at']} {entry['status']} {entry['error']}")
        if detail["runs_error"]:
            print(f"  ℹ️ {detail['runs_error']}")


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--job", help="id или точное имя задачи")
    parser.add_argument("--runs", type=int, default=10, help="число запусков для --job")
    parser.add_argument("--json", action="store_true", help="стабильный JSON для автоматизации")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs должен быть положительным")
    data, code = report(hermes_home(), args.job, args.runs)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print_human(data)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
