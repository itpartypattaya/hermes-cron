# Hermes Cron Reference

Upstream Hermes Agent 0.21.5 behaviour. Local GitOps layers, chat routing and extra fields belong in a
separate profile skill, not here. On another version, check flags with `hermes cron <command> --help`.

## CLI

```bash
hermes cron list [--all]                  # without --all: active jobs only
hermes cron status                        # gateway, heartbeat, next run
hermes cron doctor                        # built-in check of active jobs
hermes cron create "in 30m" "Remind me to check the backup" [--name ...]
hermes cron edit <id> --schedule "0 9 * * 1-5"
hermes cron pause <id>
hermes cron resume <id>                   # a paused recurring or one-shot job
hermes cron resume <id> --at <ISO>        # re-arm a COMPLETED one-shot (one-shots only)
hermes cron resume <id> --run-now         # same, for now
hermes cron run <id>                      # a real run on the next tick
hermes cron runs [<id>] [--limit 20]      # durable attempt history (alias: history)
hermes cron incidents [--state alerted]   # failure incidents
hermes cron incidents ack <incident_id>   # silence one error signature
hermes cron notepad <id> [list|get|set|delete] [key] [value]
hermes cron remove <id>                   # aliases: rm, delete
hermes cron tick                          # run all due jobs once and exit — not a dry run
```

`create` flags (`edit` takes the same; an empty string clears a field):

| Flag | Meaning |
| --- | --- |
| `--name` | human-readable name |
| `--deliver` | `origin`, `local`, a platform, `platform:chat_id[:thread_id]`, `bot-chat[:profile]` |
| `--failure-deliver` | target for failure notices only; `local` suppresses them |
| `--repeat N` | repeat limit |
| `--skill` | attach a skill (repeatable); `edit` also has `--add-skill`, `--remove-skill`, `--clear-skills` |
| `--script` | a script in `~/.hermes/scripts/`: a pre-check, or the job itself with `--no-agent` |
| `--no-agent` | no LLM: script stdout is delivered verbatim; `edit` reverses it with `--agent` |
| `--monitor-script` / `--monitor-url` | change detector; mutually exclusive, incompatible with `--no-agent` |
| `--continuity` | each run sees its own previous output; `edit` has `--no-continuity` |
| `--workdir` | working directory: its project context files join the prompt, cwd for tools |
| `--model`, `--provider` | pin a model (the agent's tool cannot set this) |
| `--pin` | pin the current main model; `edit` has `--unpin` |
| `--reasoning-effort` | `none` … `ultra` per job |
| `--interpreter` | Python from your own venv for a `.py` script |
| `--paused`, `--paused-reason` | create disabled in one write, with a reason |

## The `cronjob` tool

Actions: `create`, `list`, `update`, `pause`, `resume`, `remove`, `run`. Call `list` before
`update`/`pause`/`resume`/`remove`/`run`; never guess ids. `run` happens in the background and returns a
handle; `prompt` with `run` is transient context for that fire only. Fields for `create`/`update`:
`prompt`, `schedule`, `name`, `repeat`, `deliver`, `failure_deliver`, `skills`, `script`, `monitor` (URL or
script path), `no_agent`, `context_from`, `continuity`, `enabled_toolsets`, `workdir`, `attach_to_session`,
`pinned`; create only: `paused`, `paused_reason`. On `update`, an empty string clears a field. Since
0.21.6 `enabled_toolsets: []` is an explicit zero-tool allowlist (MCP servers included), not a clear;
omit the field for the default set.
`model`, `provider`, `reasoning_effort` and `interpreter` are CLI-only.

## Schedules

| Purpose | Form | Kind |
| --- | --- | --- |
| Once, in 30 minutes | `in 30m`, `in 2h`, `in 1d` | `once` |
| Every 30 minutes | `30m`, `every 30m`, `every hour` | `interval` |
| Days and times in words | `every monday 9am`, `weekdays at 9am`, `every day at 9am` | `cron` |
| Cron expression | `0 9 * * 1-5`; `MON-FRI`, `JAN-DEC` allowed | `cron` |
| Once, at an exact time | `2026-10-01T09:00:00+07:00` | `once` |

Times without a zone and cron expressions use `timezone` from `config.yaml`. A one-shot more than a couple
of minutes in the past is rejected. After creating a job, compare the computed `next_run_at` with the
expected time.

## Main job fields

| Field | Purpose |
| --- | --- |
| `id`, `name` | identification |
| `prompt` | self-contained instruction for a fresh agent session |
| `schedule`, `schedule_display` | normalized schedule and its label |
| `enabled`, `state`, `paused_at`, `paused_reason` | run permission and pause markers |
| `next_run_at`, `last_run_at`, `last_status`, `last_error` | runtime state |
| `last_delivery_error`, `last_delivery_unverified` | delivery failed / not confirmed by the adapter |
| `last_dispatch`, `last_fire_error` | a late or catch-up fire, a dispatch error |
| `failure_streak` | consecutive failed runs (delivery failures do not count) |
| `last_failure` | `{at, detail}` of the last failed run; survives later good runs (0.21.6+) |
| `deliver`, `failure_deliver`, `origin` | result target, failure target, where the job was created |
| `skills`, `enabled_toolsets` | the agent's skills and tools |
| `script`, `no_agent`, `monitor_script`, `monitor_url`, `monitor_state` | modes that avoid needless LLM runs |
| `context_from` | job ids whose latest output is injected (own `continuity` lives here too) |
| `model`, `provider`, `base_url` | route pin; empty means `cron.model` or the main model at fire time; a `base_url` needs an explicit `provider` and, for a provider with a stored key, the same scheme, host and port as its endpoint |
| `workdir`, `repeat` | working directory, repeat limit; re-runs after an unreachable model or a crashed dispatch do not count toward it (0.21.6+) |
| `fire_claim`, `run_claim` | scheduler leases — who took the run; never edit by hand |

A job runs when `enabled=true` and it has no pause marker (`state=paused` or `paused_at`). `origin` is
where the job was created, not automatically the right audience.

## Statuses

`last_status`: `ok`; `error` — the run failed; `delivery_failed` — the run succeeded, delivery did not;
`delivery_queued` — the reply waits in the delivery queue; `blocked_config` — preflight found no key,
skill or delivery platform, no LLM call was made; `held` — held until the provider quota recovers;
`interrupted` — stopped by a gateway shutdown.

`state`: `scheduled`, `paused`, `completed` (a finished one-shot), `error` (the next run could not be
computed — read `last_error`).

`executions.status`: `claimed`, `running`, `completed`, `failed`, `unknown`.
`executions.delivery_outcome`: `delivered`, `failed`, `not_configured`, `queued`, `suppressed` (silence or
`local`), `suppressed_acked` (incident acknowledged).

Incidents (`cron_incidents`): `detected` → `alerted` → `resolved`/`closed`. The same job with the same
error maps to the same incident; a successful run resolves open ones.

## Output markers

| Marker | Where | Effect |
| --- | --- | --- |
| empty stdout | `no_agent` | silence |
| last line `{"wakeAgent": false}` | job script | silence, the agent is not woken |
| `[SILENT]` as the whole reply | LLM job | delivery suppressed; never translate it or mix it with text |
| `[CRON_FAILURE]` on the first line | LLM job | the run is recorded as failed; the text is the evidence |

## `cron:` settings in config.yaml

| Key | Default | Meaning |
| --- | --- | --- |
| `catch_up_missed` | `true` | one catch-up run after downtime; `false` skips misses beyond the grace window |
| `allow_agent_scheduling` | `false` | give cron sessions the `cronjob` toolset |
| `preflight` | `true` | check key, skills and delivery before a run → `blocked_config` |
| `model`, `model_provider` | `""` | model for all unpinned jobs instead of the main one |
| `wrap_response` | `true` | header with the job name and a footer on delivered replies |
| `delivery.notify` | `true` | deliveries with a notification sound (Telegram) |
| `mirror_delivery` | `false` | let users reply to any delivered brief with its context |
| `max_parallel_jobs` | unbounded | due jobs run in parallel per tick |
| `output_retention` | `50` | output files kept per job |
| `script_timeout_seconds` | `3600` | timeout of a `no_agent` script |
| `failure_repeat_alert_hours` | `6` | when to repeat an alert for the same error; `0` — every time |

Model resolution: job pin → `cron.model` → the main model (`model.default`). A pinned job does not use
the global `fallback_providers` chain.

## Other mechanisms

- **Quota hold.** The provider says "retry after N s" and the whole chain is unavailable → `next_run_at`
  is parked until the window reopens, `last_status=held`.
- **Unreachable-model retry.** A network error before the first model call → re-runs after 5, 15 and
  30 minutes, then the next regular slot.
- **Gateway guard.** A job that restarts or stops the gateway is rejected at creation.
- **Webhook.** A route `platforms.webhook.extra.routes.<name>.cron_job: <id>` fires an existing job per
  event; the event body becomes transient context for that run.
- **Self-removal.** If a run removes its own job with the tool, its final reply is still delivered. From
  a cron session this is possible only with `allow_agent_scheduling: true`.

## Runtime files

| Path | Contents |
| --- | --- |
| `~/.hermes/cron/jobs.json` | jobs and their runtime state |
| `~/.hermes/cron/executions.db` | attempt history (`executions`) and incidents (`cron_incidents`) |
| `~/.hermes/cron/deliveries.db` | delivery queue through a live gateway |
| `~/.hermes/cron/notepad.db` | job notepads |
| `~/.hermes/cron/output/<job_id>/` | saved run output |
| `~/.hermes/cron/ticker_heartbeat` | `<epoch> <pid>` of the last tick; the PID is the writer |
| `~/.hermes/cron/ticker_last_success` | time of the last successful tick |
| `~/.hermes/scripts/` | job scripts; anything outside is blocked |
| `~/.hermes/logs/agent.log` | scheduler and delivery logs |
