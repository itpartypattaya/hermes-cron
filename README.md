# Hermes Cron Skill

Production-oriented, read-only-first skill for managing scheduled tasks in
[Hermes Agent](https://github.com/NousResearch/hermes-agent): reminders, recurring
digests and watchdogs.

It focuses on the standard Hermes Cron contract. It does not assume a particular
Git repository, deployment pipeline, user, chat, timezone or delivery target.

## Compatibility

Tested against Hermes Agent 0.21+ semantics:

- `in 30m` is a one-shot delay;
- `30m` and `every 30m` are recurring intervals;
- scheduler output is delivered by the job's delivery layer;
- `no_agent + script` runs without an LLM.

If your deployment pins an older Hermes version, verify schedule parsing and CLI flags
before using the examples.

## Install

```bash
git clone https://github.com/itpartypattaya/hermes-cron.git ~/.hermes/skills/hermes-cron
python3 ~/.hermes/skills/hermes-cron/scripts/cron-doctor.py
```

The doctor is standalone: it reads `~/.hermes/cron/jobs.json`, ticker stamps and,
for a selected job, `executions.db`. It never ticks, executes or changes a job.

```bash
python3 ~/.hermes/skills/hermes-cron/scripts/cron-doctor.py --job <id>
python3 ~/.hermes/skills/hermes-cron/scripts/cron-doctor.py --json
```

Exit codes: `0` healthy, `1` findings, `2` unreadable runtime state, `3` invalid
arguments or an unknown job.

## Scope

The repository contains:

- `SKILL.md` — operating rules for safe creation, changes and diagnosis;
- `scripts/cron-doctor.py` — portable, read-only health check;
- `references/` — upstream-oriented field and troubleshooting reference;
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
