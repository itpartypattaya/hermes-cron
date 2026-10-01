#!/usr/bin/env python3
"""Read-only health check for a standard Hermes cron installation.

Complements the built-in `hermes cron doctor` instead of duplicating it: runs it when the CLI is
available, then adds what it does not look at — ticker liveness including the PID that wrote the
heartbeat, disabled and paused jobs, duplicate ids, half-paused records, consecutive failures,
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


HEARTBEAT_LIMIT = 180        # тикер пишет отметку раз в минуту
SUCCESS_LIMIT = 600
OVERDUE_GRACE = 15 * 60      # тот же допуск, что у `hermes cron doctor` и `cron status`
FAILED_STATUSES = {"error", "blocked_config", "interrupted"}
BAD_DELIVERY = {"failed", "not_configured"}
OPEN_INCIDENTS = {"detected", "alerted"}
ANSI = re.compile(r"\x1b\[[0-9;]*m")


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


def read_stamp(path: Path):
    """(epoch, pid) из отметки тикера. Hermes 0.21.5 пишет `<epoch> <pid>`, старые версии — `<epoch>`."""
    try:
        fields = path.read_text(encoding="utf-8").split()
        epoch = float(fields[0])
    except (OSError, ValueError, IndexError):
        return None, None
    pid = int(fields[1]) if len(fields) > 1 and fields[1].isdigit() else None
    return epoch, pid


def writer_alive(pid):
    """Жив ли процесс, писавший heartbeat. Только Linux /proc; иначе неизвестно (None)."""
    if pid is None or not Path("/proc/self").is_dir():
        return None
    return Path(f"/proc/{pid}").exists()


def describe_age(seconds):
    if seconds is None:
        return "нет отметки"
    if seconds < 90:
        return f"{int(seconds)} с назад"
    if seconds < 5400:
        return f"{int(seconds // 60)} м назад"
    return f"{int(seconds // 3600)} ч назад"


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
    """Закреп модели: любое из provider/model/base_url. Без них задача идёт на основной модели."""
    values = [str(job.get(key)).strip() for key in ("model", "provider", "base_url")
              if isinstance(job.get(key), str) and job.get(key).strip()]
    return " · ".join(values) or None


def chat_of(deliver):
    """`platform:chat` для явного адреса `platform:chat[:thread]`, иначе None."""
    if not isinstance(deliver, str) or deliver.count(":") not in (1, 2) or "," in deliver:
        return None
    return ":".join(deliver.split(":")[:2])


def analyse(jobs, now, builtin_ran):
    findings, notes, seen = [], [], set()
    threaded = {chat_of(job.get("deliver")) for job in jobs
                if isinstance(job.get("deliver"), str) and job["deliver"].count(":") == 2}
    for job in jobs:
        job_id = str(job.get("id", ""))
        tag = f"{job_id or '?'} «{job.get('name', '?')}»"
        if not job_id:
            findings.append(f"{tag}: отсутствует id")
        elif job_id in seen:
            findings.append(f"{tag}: повторяющийся id")
        seen.add(job_id)
        schedule = job.get("schedule")
        if not isinstance(schedule, dict):
            findings.append(f"{tag}: schedule должен быть объектом")
            continue
        kind = schedule.get("kind")
        if kind not in {"cron", "interval", "once"}:
            findings.append(f"{tag}: неизвестный schedule.kind={kind!r}")
        state = job_state(job)
        active = state == "enabled"
        if state == "half-paused":
            findings.append(f"{tag}: enabled=true при маркере паузы — Hermes её не запускает, "
                            "а `cron list` показывает запланированной; реши pause или resume")
        if state == "error":
            findings.append(f"{tag}: state=error — {job.get('last_error') or 'причина не записана'}")
        if job.get("no_agent") and not job.get("script"):
            findings.append(f"{tag}: no_agent=true без script")
        streak = job.get("failure_streak")
        if active and isinstance(streak, int) and streak >= 2:
            findings.append(f"{tag}: {streak} неудачных запуска подряд")
        status = job.get("last_status")
        if active and status == "held":
            notes.append(f"{tag}: last_status=held — запуски удержаны до восстановления квоты провайдера")
        if active and not builtin_ran:
            # Эти проверки делает встроенный doctor; здесь — только когда он не запускался.
            if status in FAILED_STATUSES:
                findings.append(f"{tag}: последний запуск — {status}: {job.get('last_error') or 'без текста'}")
            if str(job.get("last_delivery_error") or "").strip():
                findings.append(f"{tag}: результат не доставлен — {job['last_delivery_error']}")
            nxt = parse_time(job.get("next_run_at"))
            if kind in {"cron", "interval"} and nxt is None:
                findings.append(f"{tag}: повторяющаяся задача без next_run_at")
            elif nxt and (now - nxt).total_seconds() > OVERDUE_GRACE:
                findings.append(f"{tag}: next_run_at просрочен более чем на 15 минут")
        if not active:
            continue
        pin = pinned_route(job)
        if pin and not job.get("no_agent"):
            notes.append(f"{tag}: закреплена модель ({pin}) — не следует за `hermes model` "
                         "и не уходит в общий fallback при отказе провайдера")
        deliver = job.get("deliver")
        if deliver in (None, "", "origin"):
            notes.append(f"{tag}: deliver=origin — сверить фактический chat/thread с адресатом текста")
        elif isinstance(deliver, str) and deliver.count(":") == 1 and chat_of(deliver) in threaded:
            notes.append(f"{tag}: deliver={deliver} без thread, хотя в этом чате есть темы — "
                         "сообщение уйдёт в общую ленту")
    return findings, notes


def connect(home: Path):
    database = home / "cron" / "executions.db"
    if not database.exists():
        return None, "executions.db не найден"
    try:
        return sqlite3.connect(f"file:{database}?mode=ro", uri=True), None
    except sqlite3.Error as exc:
        return None, f"не прочитать executions.db: {exc}"


def columns(connection, table):
    return {row[1] for row in connection.execute(f"pragma table_info({table})")}


def history(home: Path):
    """Открытые инциденты и последняя доставка по каждой задаче. Старые схемы — без этих данных."""
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
        return incidents, delivery, f"не прочитать executions.db: {exc}"
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
        return [], f"не прочитать executions.db: {exc}"
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
        return {"ran": False, "reason": "отключён флагом --no-builtin"}
    binary = hermes_binary()
    if not binary:
        return {"ran": False, "reason": "CLI hermes не найден (PATH, ~/.local/bin, HERMES_BIN)"}
    try:
        result = subprocess.run(
            [binary, "cron", "doctor"], env=os.environ | {"HERMES_HOME": str(home), "NO_COLOR": "1"},
            capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120,
        )
    except (OSError, subprocess.TimeoutExpired) as exc:
        return {"ran": False, "reason": f"не запустился: {exc}"}
    output = ANSI.sub("", (result.stdout + result.stderr)).strip()
    if result.returncode not in (0, 1):
        return {"ran": False, "reason": f"код возврата {result.returncode}: {output[-300:]}"}
    return {"ran": True, "exit_code": result.returncode, "output": output}


def ticker(home: Path):
    findings, notes = [], []
    beat, pid = read_stamp(home / "cron" / "ticker_heartbeat")
    success, _ = read_stamp(home / "cron" / "ticker_last_success")
    now = time.time()
    beat_age = None if beat is None else max(0, now - beat)
    success_age = None if success is None else max(0, now - success)
    alive = writer_alive(pid)
    if beat_age is None:
        findings.append("нет ticker_heartbeat: планировщик мог не стартовать")
    elif beat_age > HEARTBEAT_LIMIT:
        findings.append(f"heartbeat устарел: {int(beat_age)} с")
    if alive is False:
        findings.append(f"процесса {pid}, писавшего heartbeat, нет — отметка осталась от остановленного gateway")
    if success_age is None:
        notes.append("нет ticker_last_success: проверь gateway после первого тика")
    elif success_age > SUCCESS_LIMIT:
        findings.append(f"последний успешный тик устарел: {int(success_age)} с")
    data = {"heartbeat_age_seconds": beat_age, "heartbeat_pid": pid, "writer_alive": alive,
            "success_age_seconds": success_age}
    return data, findings, notes


def report(home: Path, selected: str | None, limit: int, use_builtin: bool):
    payload, error = load_json(home / "cron" / "jobs.json")
    if error:
        return {"ok": False, "error": error}, 2
    if not isinstance(payload, dict) or not isinstance(payload.get("jobs"), list):
        return {"ok": False, "error": "jobs.json должен содержать объект с массивом jobs"}, 2
    jobs = [job for job in payload["jobs"] if isinstance(job, dict)]
    now = datetime.now(timezone.utc)
    if selected:
        token = selected.casefold()
        matched = [job for job in jobs if str(job.get("id", "")).casefold() == token
                   or str(job.get("name", "")).casefold() == token]
        if not matched:
            return {"ok": False, "error": f"задача «{selected}» не найдена"}, 3
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
        findings.append("встроенный `hermes cron doctor` нашёл проблемы — см. его вывод")
    elif not builtin["ran"]:
        notes.append(f"встроенный doctor не запускался ({builtin['reason']}) — базовые проверки сделаны здесь")
    incidents, delivery, history_error = history(home)
    names = {str(job.get("id")): job.get("name", "?") for job in jobs}
    for item in incidents:
        findings.append(f"открытый инцидент {item['id']} — {item['job_id']} «{names.get(item['job_id'], '?')}»: "
                        f"{str(item['error'])[:120]} (заглушить: hermes cron incidents ack {item['id']})")
    for job in jobs:
        outcome = delivery.get(str(job.get("id")))
        if job_state(job) == "enabled" and outcome in BAD_DELIVERY:
            findings.append(f"{job.get('id')} «{job.get('name', '?')}»: последний запуск не доставлен "
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
        print("== Планировщик ==")
        pid = f", PID {scheduler['heartbeat_pid']}" if scheduler["heartbeat_pid"] else ""
        alive = {True: ", процесс жив", False: ", процесса нет", None: ""}[scheduler["writer_alive"]]
        print(f"  heartbeat: {describe_age(scheduler['heartbeat_age_seconds'])}{pid}{alive}")
        print(f"  успешный тик: {describe_age(scheduler['success_age_seconds'])}")
        builtin = data["builtin"]
        print("\n== Встроенный hermes cron doctor ==")
        if builtin["ran"]:
            for line in builtin["output"].splitlines() or ["(пустой вывод)"]:
                print(f"  {line}")
        else:
            print(f"  не запускался: {builtin['reason']}")
        print("\n== Задачи ==")
        for job in data["jobs"]:
            print(f"  {job['state']:<11} {job['id'] or '?':<14} {job['name'] or '?'} · след: {job['next_run_at'] or '—'}")
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
        print(f"  schedule: {job.get('schedule_display') or job.get('schedule')}")
        print(f"  доставка: {job.get('deliver') or 'origin'}"
              + (f" · сбои: {job['failure_deliver']}" if job.get("failure_deliver") else ""))
        print(f"  модель: {detail['pinned'] or 'основная на момент запуска'}")
        modes = [name for name, on in (("no_agent", job.get("no_agent")), ("script", job.get("script")),
                                       ("monitor", job.get("monitor_script") or job.get("monitor_url")),
                                       ("context_from", job.get("context_from"))) if on]
        print(f"  режим: {', '.join(modes) or 'LLM'}")
        print(f"  последний статус: {job.get('last_status') or '—'}"
              + (f" — {job['last_error']}" if job.get("last_error") else ""))
        for entry in detail["runs"]:
            outcome = f" · доставка: {entry['delivery_outcome']}" if entry["delivery_outcome"] else ""
            print(f"  {entry['claimed_at']} {entry['status']}{outcome} {entry['error']}".rstrip())
        for item in detail["incidents"]:
            print(f"  🔴 инцидент {item['id']} ({item['state']}): {str(item['error'])[:120]}")
        if detail["runs_error"]:
            print(f"  ℹ️ {detail['runs_error']}")


class Parser(argparse.ArgumentParser):
    def error(self, message):
        # Код 2 занят под «не прочитать runtime»; ошибки аргументов — 3, как обещает README.
        self.print_usage(sys.stderr)
        self.exit(3, f"{self.prog}: error: {message}\n")


def main():
    parser = Parser()
    parser.add_argument("--job", help="id или точное имя задачи")
    parser.add_argument("--runs", type=int, default=10, help="число запусков для --job")
    parser.add_argument("--json", action="store_true", help="стабильный JSON для автоматизации")
    parser.add_argument("--no-builtin", action="store_true", help="не запускать `hermes cron doctor`")
    args = parser.parse_args()
    if args.runs < 1:
        parser.error("--runs должен быть положительным")
    data, code = report(hermes_home(), args.job, args.runs, not args.no_builtin)
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2, default=str))
    else:
        print_human(data)
    return code


if __name__ == "__main__":
    raise SystemExit(main())
