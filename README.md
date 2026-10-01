# Hermes Cron Skill

Production-oriented, read-only-first skill for managing scheduled tasks in
[Hermes Agent](https://github.com/NousResearch/hermes-agent): reminders, recurring
digests and watchdogs.

It focuses on the standard Hermes Cron contract. It does not assume a particular
Git repository, deployment pipeline, user, chat, timezone or delivery target.

## Compatibility

Written and tested against Hermes Agent 0.21.5:

- `in 30m` is a one-shot delay; `30m` and `every 30m` are recurring intervals;
  `every monday 9am` and `weekdays at 9am` become cron expressions;
- scheduler output is delivered by the job's delivery layer; failure notices can be
  routed separately with `failure_deliver`;
- four ways not to wake the model in vain: `no_agent + script`, a `wakeAgent: false`
  pre-check, `monitor` change detection and a `[SILENT]` reply;
- jobs follow the main agent model at fire time unless pinned; a pinned job does not
  use the global fallback chain;
- a job runs only when `enabled` is true and it carries no pause marker.

On an older Hermes, verify schedule parsing and CLI flags (`hermes cron create --help`)
before using the examples. The doctor degrades gracefully on older runtime files.

## Install

```bash
git clone https://github.com/itpartypattaya/hermes-cron.git ~/.hermes/skills/hermes-cron
python3 ~/.hermes/skills/hermes-cron/scripts/cron-doctor.py
```

The doctor complements the built-in `hermes cron doctor` instead of duplicating it. It
runs the built-in check when the `hermes` CLI is found (PATH, `~/.local/bin`, or
`HERMES_BIN`), then adds what the built-in does not cover: ticker liveness including the
PID that wrote the heartbeat, disabled and paused jobs, duplicate ids, half-paused
records, consecutive failures, delivery outcomes from `executions.db`, open failure
incidents, pinned models and thread-less delivery into chats that use threads. Without
the CLI, or with `--no-builtin`, it performs the basic checks itself.

The doctor never ticks, executes or changes a job. The built-in doctor is Hermes code:
its job loader, like every scheduler tick, may repair malformed entries in `jobs.json`.

```bash
python3 ~/.hermes/skills/hermes-cron/scripts/cron-doctor.py --job <id>
python3 ~/.hermes/skills/hermes-cron/scripts/cron-doctor.py --json
python3 ~/.hermes/skills/hermes-cron/scripts/cron-doctor.py --no-builtin
```

Exit codes: `0` healthy, `1` findings, `2` unreadable runtime state, `3` invalid
arguments or an unknown job.

## Scope

The repository contains:

- `SKILL.md` — operating rules for safe creation, changes and diagnosis;
- `scripts/cron-doctor.py` — portable, read-only health check;
- `references/` — CLI, tool fields, statuses, `cron:` settings and a troubleshooting runbook;
- `tests/` — self-contained doctor regression tests.

GitOps snapshots, three-way merges, persistent configuration, organization-specific
alert routing and delivery policies are intentionally outside this public skill. Keep
those rules in a private profile that is loaded alongside `hermes-cron`.

## Test

```bash
python3 -m unittest discover -s tests -v
```

## License

[MIT](LICENSE)
