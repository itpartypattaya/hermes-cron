---
name: hermes-cron
description: Create, change, pause and debug Hermes cron jobs.
version: 1.3.0
author: "Anton Vaskov (itpartypattaya), https://t.me/passone"
license: MIT
compatibility: Hermes Agent >= 0.21 (checked against 0.21.6)
allowed-tools: cronjob terminal read_file
tags: [cron, scheduler, reminders, watchdogs, ops]
---

# Hermes Cron Skill

Operating rules for Hermes cron: reminders, recurring digests and watchdogs. It covers choosing the
right lifecycle action, keeping jobs cheap and quiet, verifying every change and diagnosing a job
that did not fire, fired twice or went to the wrong chat. It does not cover the operating system's cron daemon, GitOps
layers or delivery policies of a particular installation — keep those in a separate local skill.

## When to Use

- The user asks for a reminder, a recurring digest, a watchdog or "do X every …".
- The user asks to pause, resume, disable, remove or change an existing job.
- A job did not fire, fired at the wrong time, fired twice, or delivered to the wrong place.
- You are asked what is scheduled, or whether a change actually took effect.

## Prerequisites

- The `cronjob` tool (Hermes cron toolset) or the `hermes cron` CLI through `terminal`.
- A running gateway: the scheduler ticks inside it once a minute.
- Python 3 for the bundled read-only doctor. No network access, no API keys.

## How to Run

Start every cron task with the read-only doctor through `terminal`:

```bash
python3 "${HERMES_SKILL_DIR}/scripts/cron-doctor.py"               # all jobs
python3 "${HERMES_SKILL_DIR}/scripts/cron-doctor.py" --job <id>    # one job + recent runs
python3 "${HERMES_SKILL_DIR}/scripts/cron-doctor.py" --json        # machine-readable
python3 "${HERMES_SKILL_DIR}/scripts/cron-doctor.py" --no-builtin  # strictly read-only
```

It runs the built-in `hermes cron doctor` and adds what that check does not cover: ticker stamps,
disabled, malformed and half-paused jobs, failure streaks, the delivery outcome of each job's latest
completed run, open failure incidents, one error across three or more jobs in the last 24 h, pinned
models, a cron store that cannot be written or is almost full, and the last failure of a job that has
recovered since. The doctor never runs, edits or removes a
job; the built-in check it calls loads `jobs.json` through Hermes, which may repair malformed
entries — `--no-builtin` skips it. Exit codes: `0` healthy, `1` findings (also for `--job`),
`2` unreadable runtime state, `3` bad arguments or an unknown job.

## Quick Reference

| The user wants | Action | Check afterwards |
| --- | --- | --- |
| "Be quiet until a date" | `pause`; say who resumes it and when | `enabled=false`, `state=paused`, resume plan stated |
| "Create it, but not yet" | create with `paused` + `paused_reason` | created disabled in one write |
| "Remind me in 30 minutes" | schedule `in 30m` | kind `once`, exact time |
| "Every 30 minutes" | `30m` or `every 30m` | kind `interval`, next run |
| "Weekdays at 9" | `0 9 * * 1-5` or `weekdays at 9am` | `next_run_at` in the configured `timezone` |
| "Run it now" | `run` (real run, in the background) | result arrived where expected |
| "Repeat that one-off reminder" | `hermes cron resume <id> --at <ISO>` (one-shots only) | new `next_run_at` |
| "Delete it" | `remove`, plus any persistent definition | it does not come back after a sync |

How to keep a job from waking the model in vain:

| Situation | Mode | How it stays silent |
| --- | --- | --- |
| Text is known in advance; alert watchdog | `no_agent: true` + `script` — stdout is the message | empty stdout or `{"wakeAgent": false}` |
| Fresh data needed, real work is rare | `script` without `no_agent` — a pre-check feeding the prompt | last line `{"wakeAgent": false}` |
| "Tell me when this changes" | `monitor` — a script or an http(s) URL | unchanged output skips the agent run |
| The model must decide, but not always report | a normal LLM job | reply exactly `[SILENT]` |

Memory between runs: `continuity: true` shows each run its own previous output; `context_from`
injects another job's last output; `hermes cron notepad <id> set <key> <value>` keeps small
cursors (16 KB per value, 64 KB per job) that are injected into the prompt.

## Procedure

1. Run the doctor. A list without `--all` shows active jobs only: "not in the list" is not "absent".
2. Look for a duplicate by meaning — schedule, delivery target, script, purpose — not by name.
   "Set a reminder" often means an existing job is paused or broken; update it instead.
3. Pick the lifecycle action and the cheapest mode from the tables above.
4. Write a self-contained `prompt`: a cron session has no chat history and cannot ask questions.
   Ask the model to "write the summary", not to "send" it — the scheduler delivers the final reply.
5. Set `deliver` deliberately. Omitted means the chat where the job was created (`origin`), not the
   person the text is for. Explicit form: `platform:chat_id:thread_id`; also `local`, `all`, or
   several targets separated by commas. Route failure notices with `failure_deliver`.
6. Give only the `enabled_toolsets` the job needs; omit the field for the default set — `[]` means
   no tools at all. Leave the model unpinned unless the user asks.
7. After the change, read the job back and run the doctor with `--job <id>`. For a script job, run
   the script once on its own with safe input; for a watchdog, test both silence and the alert.
8. Report the observed fact: what changed, when it runs next and where the result will go.

## Pitfalls

- **Tool success is not delivery.** `success` means the record changed. Read it back.
- **A bare duration repeats.** `30m` is an interval; a one-off delay needs `in 30m`.
- **Half-pause.** A job runs only if `enabled=true` and it has no pause marker (`state=paused` or
  `paused_at`). `enabled=true` plus a marker never fires, yet `cron list` shows it as scheduled.
- **Missed slots.** A slot missed while paused fires once after `resume` (or is skipped by
  `cron.catch_up_missed: false`); a gateway restart can also trigger one catch-up run.
- **No date-based resume for recurring jobs.** `resume --at` and `--run-now` re-arm one-shots only.
- **Cron sessions cannot schedule.** The `cronjob` toolset is off inside cron runs
  (`cron.allow_agent_scheduling: false`); `messaging` and `clarify` are always off. A cron run that
  "resumed" another job in text did nothing.
- **Pinned models skip fallback.** A job with `model`, `provider` or `base_url` set ignores
  `hermes model` and does not use the global fallback chain; unpinned jobs follow `cron.model`, if set,
  otherwise the main model.
- **An empty toolset list is a zero-tool job.** Since 0.21.6 `enabled_toolsets: []` keeps the agent
  without any tools; it no longer clears the restriction. To return to the default, remove the field.
- **Monitor output must be stable.** A timestamp in the output makes every tick look changed. The
  first tick always wakes the agent. `monitor` cannot be combined with `no_agent`.
- **Scripts live in `~/.hermes/scripts/`.** Paths outside it are blocked; `.sh`/`.bash` run in
  bash, everything else in Python; use `--interpreter` for your own venv.
- **Failures alert once per incident.** Repeats wait `cron.failure_repeat_alert_hours` (6 h);
  `hermes cron incidents ack <id>` silences one. `[CRON_FAILURE]` on the first line of a reply
  records the run as failed. `last_status=held` is a quota hold, not a bug.
- **One error in many jobs is one fault.** When the doctor reports the same error across several
  jobs, fix the scheduler or its host first; editing the jobs one by one changes nothing.
  `last_status` and `failure_streak` reset on the next good run; read the run history, or
  `last_failure` (0.21.6+), which keeps the time and reason of the last failure.
- **An unwritable store skips jobs.** On a full disk, a read-only mount or wrong permissions, Hermes
  0.21.6+ skips due jobs it cannot record; `hermes cron status` leads with a warning. After the fix
  each skipped job fires once — nothing to resume.
- **Persistent writers win.** If a removed or disabled job comes back, a sync or deployment rewrites
  `jobs.json`; change the source of truth, not only the runtime file.

## Verification

```bash
python3 "${HERMES_SKILL_DIR}/scripts/cron-doctor.py" --job <id>
```

Exit code `0`: the job shows the expected state, schedule, delivery target and next run, and its
latest completed run has `delivery: delivered` (or the intended silence). Field and CLI reference:
`references/reference.md`; symptom runbook: `references/troubleshooting.md`.
