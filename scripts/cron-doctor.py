#!/usr/bin/env python3
"""Диагностика cron-задач Hermes: состояние, аномалии, дрейф runtime ↔ main.

Читает только. Ничего не мутирует и не запускает — безопасен в любой момент,
в том числе когда непонятно, что вообще происходит.

Отвечает на четыре вопроса, которые иначе приходится собирать руками из трёх
файлов и базы:
  1. жив ли планировщик (возраст heartbeat тикера);
  2. в каком состоянии каждая задача (включена / на паузе / когда следующий запуск);
  3. нет ли записей, противоречащих самим себе (enabled=true при state=paused —
     такая задача выглядит выключенной и при этом запускается);
  4. что разойдётся с одобренным в main jobs.tracked.json — то есть какие правки
     ночной автосинк применит, предложит в PR или откатит.

Запуск:
  cron-doctor.py                  сводка по всем задачам
  cron-doctor.py --job <id|имя>   одна задача + последние запуски из executions.db
  cron-doctor.py --all            показать и одноразовые, уже отработавшие
  cron-doctor.py --runs N         сколько запусков печатать для --job (по умолчанию 10)

HERMES_HOME (или HERMES_DIR) — корень .hermes, по умолчанию ~/.hermes.
Код возврата: 0 — аномалий нет, 1 — есть (список печатается).
"""
from __future__ import annotations

import argparse
import importlib.util
import json
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone


# Вывод русский и с эмодзи: под Windows-консолью в cp1251 печать иначе падает
# UnicodeEncodeError, и диагностика умирает на выводе, а не на диагностике.
for _stream in (sys.stdout, sys.stderr):
    try:
        _stream.reconfigure(encoding="utf-8")
    except (AttributeError, ValueError):
        pass


def home() -> str:
    return (os.environ.get("HERMES_HOME") or os.environ.get("HERMES_DIR")
            or os.path.expanduser("~/.hermes"))


def _load_volatile() -> set:
    """Граница «структура vs счётчики» — одна на весь конвейер, из cron-snapshot.py.

    Своя копия списка здесь уже разъехалась молча: доктор считал `model_snapshot`
    и `provider_snapshot` рантаймом, а снимок их держит (ядро пишет их при
    создании/правке задачи — это снимок намерения, не счётчик), и наоборот —
    `fire_claim`/`run_claim` доктор вырезал, а снимок нет, пока Codex не указал
    на это в PR #99. Без общей границы «дрейф с main» в выводе доктора — угадайка,
    поэтому отсутствие снимка — отказ, а не тихий откат к своему списку.
    Ищем сначала в репозитории (skills/hermes-cron/scripts → корень), потом в HERMES_HOME.
    """
    here = os.path.dirname(os.path.abspath(__file__))
    candidates = [
        os.path.normpath(os.path.join(here, "..", "..", "..", "scripts", "cron-snapshot.py")),
        os.path.join(home(), "scripts", "cron-snapshot.py"),
    ]
    for path in candidates:
        if not os.path.isfile(path):
            continue
        spec = importlib.util.spec_from_file_location("cron_snapshot", path)
        if spec is None or spec.loader is None:
            continue
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        return set(module.VOLATILE)
    print("⚠️  cron-snapshot.py не найден (" + ", ".join(candidates) + ") — "
          "без его списка волатильных полей дрейф с main не посчитать", file=sys.stderr)
    raise SystemExit(2)


# Поля, которые cron-snapshot.py вырезает как волатильные: по ним сверять runtime
# с одобренным снимком бессмысленно — их там нет по построению.
VOLATILE = _load_volatile()


def load(path):
    try:
        with open(path, encoding="utf-8-sig") as f:
            return json.load(f)
    except FileNotFoundError:
        return None
    except (OSError, json.JSONDecodeError) as exc:
        print(f"⚠️  не прочитать {path}: {exc}", file=sys.stderr)
        return None


def parse_dt(value):
    if not isinstance(value, str) or not value:
        return None
    try:
        dt = datetime.fromisoformat(value)
    except ValueError:
        return None
    return dt if dt.tzinfo else dt.astimezone()


def human_delta(dt, now):
    """«через 6 ч 12 м» / «2 д назад» — горизонт, а не голая дата."""
    if dt is None:
        return "—"
    secs = (dt - now).total_seconds()
    ahead = secs >= 0
    secs = abs(secs)
    if secs < 90:
        out = f"{int(secs)} с"
    elif secs < 5400:
        out = f"{int(secs // 60)} м"
    elif secs < 172800:
        h, m = divmod(int(secs // 60), 60)
        out = f"{h} ч {m} м" if m else f"{h} ч"
    else:
        out = f"{int(secs // 86400)} д"
    return f"через {out}" if ahead else f"{out} назад"


def short(value, width):
    value = "" if value is None else str(value)
    return value if len(value) <= width else value[: width - 1] + "…"


def ticker_report(now):
    """Возраст heartbeat планировщика. Мёртвый тикер = не идёт ничего."""
    out, problems = [], []
    for label, name, limit in (
        ("тик", "ticker_heartbeat", 180),
        ("успешный тик", "ticker_last_success", 600),
    ):
        path = os.path.join(home(), "cron", name)
        try:
            age = time.time() - float(open(path, encoding="utf-8").read().strip())
        except (OSError, ValueError):
            out.append(f"  {label}: отметки нет ({name})")
            problems.append(f"нет отметки планировщика {name} — тикер мог не стартовать")
            continue
        mark = "✅" if age <= limit else "🔴"
        out.append(f"  {mark} {label}: {int(age)} с назад")
        if age > limit:
            problems.append(
                f"{label} планировщика {int(age)} с назад (норма ≤{limit} с) — "
                "проверь hermes-gateway, задачи сейчас не запускаются"
            )
    return out, problems


def job_kind(job):
    if job.get("no_agent"):
        return "скрипт"
    if job.get("script"):
        return "прекчек+LLM"
    return "LLM"


def state_mark(job):
    enabled = job.get("enabled", True)
    state = job.get("state")
    if enabled and state == "paused":
        return "⁉️ ПОРЧА"
    if not enabled and state == "paused":
        return "⏸ пауза"
    if not enabled:
        return "🚫 выкл"
    if state == "error":
        return "🔴 ошибка"
    return "✅ вкл"


def anomalies(jobs, now):
    """Разделяет находки на «сломано» (🔴) и «мусор/к сведению» (ℹ️).

    Смешивать их нельзя: если раздел проблем каждый раз печатает десяток безобидных
    записей, его перестают читать — и настоящая поломка проезжает вместе с шумом.
    """
    found, notes, dead_oneshots = [], [], []

    # Есть ли в этом чате ХОТЬ ОДНА задача с веткой. Только это и отличает чат с
    # топиками от обычной группы: по jobs.json иначе не узнать, а ругаться на
    # «потерянную ветку» там, где веток нет (чат Евы), — чистый ложный сигнал.
    chats_with_threads = set()
    for j in jobs:
        dv = j.get("deliver")
        if isinstance(dv, str) and dv.count(":") >= 2:
            chats_with_threads.add(dv.rsplit(":", 1)[0])

    for j in jobs:
        jid, name = j.get("id", "?"), j.get("name", "?")
        tag = f"{jid} «{short(name, 40)}»"
        enabled = j.get("enabled", True)
        state = j.get("state")
        sched = j.get("schedule") or {}
        kind = sched.get("kind")

        if enabled and state == "paused":
            found.append(
                f"{tag}: enabled=true при state=paused — задача выглядит выключенной, "
                f"но запускается (пауза с {j.get('paused_at') or '?'})"
            )
        if not enabled and state == "scheduled":
            found.append(f"{tag}: enabled=false при state=scheduled — выключена, но помечена запланированной")
        if state == "error":
            found.append(f"{tag}: state=error — {short(j.get('last_error') or 'причина не записана', 90)}")
        if j.get("no_agent") and not j.get("script"):
            found.append(f"{tag}: no_agent=true без script — запускать нечего, тик будет падать")
        if j.get("last_status") == "error" and enabled:
            found.append(f"{tag}: последний запуск завершился ошибкой — {short(j.get('last_error') or '', 80)}")

        nxt = parse_dt(j.get("next_run_at"))
        if enabled and state != "paused":
            # При state=error отсутствие next_run_at — уже описанное следствие, а не
            # отдельная находка: дублировать одну поломку двумя строками значит
            # раздувать раздел, который должен читаться целиком.
            if kind in {"cron", "interval"} and nxt is None and state != "error":
                found.append(f"{tag}: повторяющаяся задача без next_run_at — пересчитается на старте gateway")
            elif nxt is not None and (now - nxt).total_seconds() > 3600:
                found.append(
                    f"{tag}: next_run_at просрочен ({human_delta(nxt, now)}) при живом тикере — "
                    "задача чем-то заблокирована"
                )
            if kind == "once" and nxt is None and not j.get("last_run_at"):
                dead_oneshots.append(jid)

        # Доставка в общую ленту чата, у которого есть ветки. Иногда так и задумано
        # (дайджест в основную ленту), поэтому это повод посмотреть, а не поломка.
        deliver = j.get("deliver")
        if isinstance(deliver, str) and deliver.count(":") == 1 and deliver in chats_with_threads:
            notes.append(
                f"{tag}: deliver={deliver} без :thread_id, хотя в этом чате есть ветки — "
                "сообщение уйдёт в общую ленту; убедись, что так задумано"
            )

    if dead_oneshots:
        notes.append(
            f"мёртвых одноразовых записей: {len(dead_oneshots)} ({', '.join(dead_oneshots)}) — "
            "их время прошло, next_run_at нет, запусков нет: они уже не выстрелят. "
            "Автосинк воскрешает их из jobs.tracked.json каждую ночь, поэтому убрать "
            "их можно только правкой снимка в main"
        )
    return found, notes


def clean(obj):
    """Срезать волатильные ключи на любой глубине — так же, как cron-snapshot.py.

    Рекурсия обязательна: `repeat.completed` — счётчик, лежащий ВНУТРИ структурного
    `repeat`. Сравнение только по верхнему уровню объявляло бы расходящейся с main
    каждую задачу, которая хоть раз запускалась, — то есть шум вместо сигнала.
    """
    if isinstance(obj, dict):
        return {k: clean(v) for k, v in obj.items() if k not in VOLATILE}
    if isinstance(obj, list):
        return [clean(v) for v in obj]
    return obj


def drift(runtime_jobs, tracked_jobs, baseline_jobs):
    """Что разойдётся с main и чем это кончится в 04:00.

    С 05.08.2026 `cron-apply-tracked.py` мёржит трёхсторонне, поэтому «расходится»
    больше не означает «откатят». Исход читается по базе (`cron/jobs.baseline.json`
    — снимок на конец прошлого синка): двигался main или правили рантайм. Печатать
    «вернёт значения из main» без этой проверки — врать в обе стороны сразу.
    """
    if tracked_jobs is None:
        return ["jobs.tracked.json не найден — сверить с одобренным состоянием нечем"]
    approved = {str(j.get("id")): clean(j) for j in tracked_jobs if j.get("id")}
    base = ({str(j.get("id")): clean(j) for j in baseline_jobs if j.get("id")}
            if baseline_jobs is not None else None)
    live = {str(j.get("id")): j for j in runtime_jobs if j.get("id")}
    lines = []

    for jid, job in live.items():
        name = short(job.get("name", "?"), 40)
        if jid not in approved:
            lines.append(f"  ➕ {jid} «{name}» — есть в рантайме, нет в main (автосинк предложит в PR)")
            continue
        cleaned = clean(job)
        diff = sorted(k for k, v in approved[jid].items() if cleaned.get(k) != v)
        if not diff:
            continue
        # Пауза — рантайм-решение, и оверлей её не отменяет (cron-apply-tracked.py
        # не накладывает enabled=true поверх приостановленной задачи). Сообщать про
        # «вернёт значения из main» тут значило бы пугать несуществующим откатом.
        if diff == ["enabled"] and job.get("state") == "paused" and not job.get("enabled", True):
            lines.append(f"  ⏸ {jid} «{name}» — пауза держится: в main задача включена, откатывать её автосинк не будет")
            continue
        if base is None or jid not in base:
            lines.append(
                f"  ⚠️  {jid} «{name}» — расходится с main по: {', '.join(diff)} "
                "(базы 3-way ещё нет — ближайший автосинк применит значения из main)"
            )
            continue
        was = base[jid]
        kept = [k for k in diff if approved[jid].get(k) == was.get(k)]
        conflict = [k for k in diff if approved[jid].get(k) != was.get(k)
                    and cleaned.get(k) != was.get(k)]
        from_main = [k for k in diff if k not in kept and k not in conflict]
        if kept:
            lines.append(
                f"  ✏️  {jid} «{name}» — правлено в рантайме: {', '.join(sorted(kept))} "
                "(main не двигался — автосинк сохранит и предложит в PR)"
            )
        if from_main:
            lines.append(
                f"  ⬇️  {jid} «{name}» — одобрено в main: {', '.join(sorted(from_main))} "
                "(автосинк применит значения из main)"
            )
        if conflict:
            lines.append(
                f"  🔴 {jid} «{name}» — конфликт по: {', '.join(sorted(conflict))} "
                "(правили и в рантайме, и в main — победит main, рантайм-правка потеряется)"
            )
    for jid, job in approved.items():
        if jid not in live:
            lines.append(
                f"  ♻️  {jid} «{short(job.get('name', '?'), 40)}» — есть в main, нет в рантайме "
                "(автосинк восстановит задачу в 04:00)"
            )

    paused = [j for j in live.values() if j.get("state") == "paused"]
    if paused:
        lines.append(
            f"  ℹ️  на паузе {len(paused)} — пауза живёт только на диске сервера и в main не "
            "отражается; пересборка с нуля поднимет эти задачи включёнными"
        )
    return lines or ["  расхождений с main нет"]


def executions(job_id, limit):
    path = os.path.join(home(), "cron", "executions.db")
    if not os.path.exists(path):
        return ["  executions.db не найден"]
    try:
        con = sqlite3.connect(f"file:{path}?mode=ro", uri=True)
        rows = con.execute(
            "select claimed_at, started_at, finished_at, status, coalesce(error,'') "
            "from executions where job_id = ? order by claimed_at desc limit ?",
            (job_id, limit),
        ).fetchall()
        con.close()
    except sqlite3.Error as exc:
        return [f"  не прочитать executions.db: {exc}"]
    if not rows:
        return ["  запусков не зафиксировано"]
    out = []
    for claimed, started, finished, status, error in rows:
        mark = {"completed": "✅", "failed": "🔴", "running": "⏳"}.get(status, "•")
        tail = f" — {short(error, 70)}" if error else ""
        out.append(f"  {mark} {short(claimed, 25):<25} {status}{tail}")
    return out


def main() -> int:
    ap = argparse.ArgumentParser(add_help=True)
    ap.add_argument("--job", help="id или имя задачи: подробности + история запусков")
    ap.add_argument("--all", action="store_true", help="показывать и отработавшие одноразовые")
    ap.add_argument("--runs", type=int, default=10, help="сколько запусков печатать для --job")
    args = ap.parse_args()

    now = datetime.now(timezone.utc).astimezone()
    runtime = load(os.path.join(home(), "cron", "jobs.json"))
    if not isinstance(runtime, dict) or not isinstance(runtime.get("jobs"), list):
        print("🔴 cron/jobs.json не прочитан или имеет неверную структуру", file=sys.stderr)
        return 1
    jobs = runtime["jobs"]
    tracked = load(os.path.join(home(), "cron", "jobs.tracked.json"))
    tracked_jobs = tracked.get("jobs") if isinstance(tracked, dict) else None
    # База 3-way: снимок на конец прошлого синка. Без неё исход дрейфа
    # неизвестен — доктор так и скажет, вместо того чтобы гадать.
    baseline = load(os.path.join(home(), "cron", "jobs.baseline.json"))
    baseline_jobs = baseline.get("jobs") if isinstance(baseline, dict) else None

    if args.job:
        ref = args.job.strip().lower()
        match = [j for j in jobs if str(j.get("id", "")).lower() == ref
                 or str(j.get("name", "")).lower() == ref]
        if not match:
            print(f"задача «{args.job}» не найдена; запусти без --job, чтобы увидеть список")
            return 1
        for j in match:
            print(f"== {j.get('id')} «{j.get('name')}» ==")
            print(f"  состояние     {state_mark(j)}  (enabled={j.get('enabled', True)}, state={j.get('state')})")
            if j.get("paused_at"):
                print(f"  пауза с       {j['paused_at']}")
            print(f"  расписание    {j.get('schedule_display') or (j.get('schedule') or {}).get('display')}")
            print(f"  тип           {job_kind(j)}" + (f" · {j['script']}" if j.get("script") else ""))
            print(f"  доставка      {j.get('deliver') or 'origin'}")
            nxt, last = parse_dt(j.get("next_run_at")), parse_dt(j.get("last_run_at"))
            print(f"  следующий     {j.get('next_run_at') or '—'}  ({human_delta(nxt, now)})")
            print(f"  последний     {j.get('last_run_at') or '—'}  ({human_delta(last, now)}), статус {j.get('last_status') or '—'}")
            if j.get("last_error"):
                print(f"  последняя ошибка {short(j['last_error'], 120)}")
            rep = j.get("repeat") or {}
            print(f"  запусков      {rep.get('completed', 0)}" + (f" из {rep['times']}" if rep.get("times") else " (без лимита)"))
            if j.get("skills"):
                print(f"  скиллы        {', '.join(j['skills'])}")
            if j.get("enabled_toolsets") is not None:
                print(f"  тулсеты       {j['enabled_toolsets'] or '[] (пусто)'}")
            print(f"\n  последние запуски (executions.db, до {args.runs}):")
            for line in executions(str(j.get("id")), args.runs):
                print(line)
        return 0

    print("== Планировщик ==")
    tick_lines, tick_problems = ticker_report(now)
    print("\n".join(tick_lines))

    # Отработавшие одноразовые — шум: их время прошло, повторно они не выстрелят.
    # Прячем по факту (запуск был / момент прошёл), а не по флагу enabled: у части
    # старых записей он так и остался true.
    def spent_oneshot(j):
        if (j.get("schedule") or {}).get("kind") != "once":
            return False
        if j.get("last_run_at"):
            return True
        run_at = parse_dt(((j.get("schedule") or {}).get("run_at")))
        return run_at is not None and run_at < now

    shown = [j for j in jobs if args.all or not spent_oneshot(j)]
    hidden = len(jobs) - len(shown)
    print(f"\n== Задачи: {len(jobs)} всего, включено {sum(1 for j in jobs if j.get('enabled', True))}"
          + (f", скрыто отработавших одноразовых {hidden} (--all покажет)" if hidden else "") + " ==")
    for j in sorted(shown, key=lambda x: (not x.get("enabled", True), str(x.get("name", "")))):
        nxt = parse_dt(j.get("next_run_at"))
        print(f"  {state_mark(j):<9} {j.get('id','?'):<13} {short(j.get('name'), 42):<42} "
              f"{short(j.get('schedule_display'), 18):<18} "
              f"{short(job_kind(j), 11):<11} след: {human_delta(nxt, now)}")

    print("\n== Дрейф runtime ↔ main ==")
    print("\n".join(drift(jobs, tracked_jobs, baseline_jobs)))

    found, notes = anomalies(jobs, now)
    problems = tick_problems + found
    print("\n== Аномалии ==")
    if problems:
        for p in problems:
            print(f"  🔴 {p}")
    else:
        print("  не найдено")
    if notes:
        print("\n== К сведению (не поломка) ==")
        for n in notes:
            print(f"  ℹ️  {n}")
    return 1 if problems else 0


if __name__ == "__main__":
    raise SystemExit(main())
