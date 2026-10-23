# Hermes Cron Reference

Upstream Hermes Agent 0.22.4 behaviour (written against 0.21.5, checked at every release since). Local GitOps layers, chat routing and extra fields belong in a
separate profile skill, not here. On another version, check flags with `hermes cron <command> --help`.

## CLI

```bash
hermes cron list [--all]                  # without --all: enabled and paused jobs; --all adds disabled
hermes cron status                        # gateway, heartbeat, next run
hermes cron doctor                        # built-in check of active jobs
hermes cron create "in 30m" "Remind me to check the backup" [--name ...]
hermes cron edit <id> --schedule "0 9 * * 1-5"
hermes cron pause <id>
hermes cron resume <id>                   # a paused recurring or one-shot job
hermes cron resume <id> --at <ISO>        # re-arm a COMPLETED one-shot (one-shots only)
hermes cron resume <id> --run-now         # same, for now
hermes cron run <id>                      # a real run now, waits for the result; exit 1 if it ran in place and failed (0.22.4+)
hermes cron runs [<id>] [--limit 20]      # durable attempt history (alias: history)
hermes cron incidents [--state alerted]   # failure incidents
hermes cron incidents ack <incident_id>   # silence one error signature
hermes cron notepad <id> [list|get|set|delete] [key] [value]
hermes cron remove <id>                   # aliases: rm, delete
hermes cron tick                          # run due jobs once and exit — not a dry run; without a ready
                                          # gateway only no_agent jobs run (0.22.4+), see below
```

`create` flags (`edit` takes the same; an empty string clears a field):

| Flag | Meaning |
| --- | --- |
| `--name` | human-readable name |
| `--deliver` | `origin`, `local`, a platform, `platform:chat_id[:thread_id]`, `bot-chat[:profile]` |
| `--failure-deliver` | target for failure notices only; `local` suppresses them |
| `--repeat N` | repeat limit |
| `--skill` | attach a skill (repeatable); `edit` also has `--add-skill`, `--remove-skill`, `--clear-skills` |
| `--script` | a file in `~/.hermes/scripts/`: a pre-check, or the job itself with `--no-agent`; a value with whitespace that names no file (`echo hi`) is refused (0.22.4+) |
| `--no-agent` | no LLM: script stdout is delivered verbatim; `edit` reverses it with `--agent` |
| `--monitor-script` / `--monitor-url` | change detector; mutually exclusive, incompatible with `--no-agent` |
| `--continuity` | each run sees its own previous output; `edit` has `--no-continuity` |
| `--workdir` | working directory: its project context files join the prompt, cwd for tools |
| `--model`, `--provider` | pin a model (the agent's tool cannot set this) |
| `--pin` | pin the current main model; `edit` has `--unpin` |
| `--reasoning-effort` | `none` … `ultra` per job |
| `--interpreter` | Python from your own venv for a `.py` script |
| `--paused`, `--paused-reason` | create disabled in one write, with a reason |

## The `cronjob_manage` tool

The old name `cronjob` still works; the toolset is called `cronjob`. Hermes loads this tool on demand
(`tools.tool_search.defer`): when it is missing from the tool list, call `tool_search` for it first.

Actions: `create`, `list`, `update`, `pause`, `resume`, `remove`, `run`. Call `list` before
`update`/`pause`/`resume`/`remove`/`run`; never guess ids. `run` happens in the background and returns a
handle; `prompt` with `run` is transient context for that fire only. Fields for `create`/`update`:
`prompt`, `schedule`, `name`, `repeat`, `deliver`, `failure_deliver`, `skills`, `script`, `monitor` (URL or
script path), `no_agent`, `context_from`, `continuity`, `enabled_toolsets`, `workdir`, `attach_to_session`,
`pinned`; create only: `paused`, `paused_reason`. Clearing on `update`: string fields (`failure_deliver`,
`monitor`, `workdir`, `script`) take `""`; list fields (`skills`, `context_from`) take `[]`; a blank
`name` is ignored, not cleared; `continuity: false` turns continuity off. Since 0.21.6
`enabled_toolsets: []` is an explicit zero-tool allowlist (MCP servers included), not a clear.
Omitting the field means the default set only on create: on update it keeps the stored list, and no
tool argument or CLI flag removes it. The web dashboard saves an empty toolsets field as no restriction
(so it cannot set `[]`); otherwise recreate the job.
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
| `last_dispatch`, `last_fire_error` | a late or catch-up fire; `{at, detail}` of a fire that was not handed over (e.g. `Skipped: the gateway is not running, …`), cleared by the next successful run |
| `failure_streak` | consecutive failed runs (delivery failures do not count) |
| `last_failure` | `{at, detail}` of the last failed run; survives later good runs (0.21.6+) |
| `deliver`, `failure_deliver`, `origin` | result target, failure target, where the job was created |
| `skills`, `enabled_toolsets` | the agent's skills and tools |
| `script`, `no_agent`, `monitor_script`, `monitor_url`, `monitor_state` | modes that avoid needless LLM runs |
| `context_from` | job ids whose latest output is injected (own `continuity` lives here too) |
| `model`, `provider`, `base_url` | route pin (`base_url` has no CLI flag: set it in `jobs.json`); empty means `cron.model` or the main model at fire time; a `base_url` needs an explicit `provider` and, for a provider with a stored key, the same scheme, host and port as its endpoint |
| `workdir`, `repeat` | working directory, repeat limit; re-runs after an unreachable model or a crashed dispatch do not count toward it (0.21.6+) |
| `fire_claim`, `run_claim` | scheduler leases — who took the run; never edit by hand |
| `pending_slot`, `last_delivery_queued`, `canonical_completions` | scheduler bookkeeping: a recurring slot handed to the dispatcher, a delivery handed to the queue, ids of completed gateway runs (0.22.4+); never edit by hand |

A job runs when `enabled=true` and it has no pause marker (`state=paused` or `paused_at`). `origin` is
where the job was created, not automatically the right audience.

## Statuses

`last_status`: `ok`; `error` — the run failed; `delivery_failed` — the run succeeded, delivery did not;
`delivery_queued` — the reply waits in the delivery queue (0.22.4+ usually turns it into `ok` or
`delivery_failed` once the gateway drains it); `blocked_config` — preflight found no key,
skill or delivery platform, no LLM call was made. A quota hold and a gateway shutdown both store
`error`: the hold adds `quota_hold_until`, the shutdown says so in `last_error`.

`state`: `scheduled`, `paused`, `completed` (a finished one-shot), `error` (the next run could not be
computed — read `last_error`).

`executions.status`: `claimed`, `running`, `completed`, `failed`, `unknown`. Since 0.22.4 an `unknown`
run can still become `completed` or `failed` when its receipt is reconciled.
`executions.delivery_outcome`: `delivered`, `failed`, `not_configured`, `queued`, `suppressed` (silence or
`local`), `suppressed_acked` (incident acknowledged). Since 0.22.4 a drain writes the real outcome over
`queued` (on a completed, failed or unknown run) and books a send whose owner died as `failed`; the job
record follows only while that run is still its latest. A `queued` that stays is a reason to look at
the gateway and the run, not proof that the gateway is down.

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
| `retry_unreachable` | `true` | re-run a recurring job that never reached the model (see below) |

Model resolution: job pin → `cron.model` → the main model (`model.default`) → since 0.22.5, a claimed
welcome-trial model. A pinned job does not use
the global `fallback_providers` chain.

## Other mechanisms

- **Quota hold.** The provider says "retry after N s" and the whole chain is unavailable → `next_run_at`
  is parked until the window reopens; `quota_hold_until` holds that time and `last_status` stays `error`.
  Recurring jobs only; one alert on entering the hold.
- **Unreachable-model retry.** A network error before the first model call → re-runs after 5, 15 and
  30 minutes, then the next regular slot. Recurring jobs only (a one-shot has spent its run); the failure
  alert waits while a retry is pending. Off: `cron.retry_unreachable: false`.
- **Gateway guard.** A job that restarts or stops the gateway is rejected at creation.
- **Runs go through the gateway (0.22.4+).** An agent job is admitted and run by the gateway that owns
  the profile's cron store. A tick outside the gateway (`hermes cron tick` from a system crontab) runs
  only `no_agent` jobs while no gateway is ready (absent, starting, draining or not answering): the
  others get `last_fire_error` `Skipped: the gateway is <state> …`, no run is recorded and the refused
  slot is not spent. `HERMES_TUI_GATEWAY_URL` skips that readiness check.
- **Auto-pause after an unverified fire (0.22.4+).** When Hermes cannot verify that the gateway
  admitted a fire, it marks the run `unknown` and pauses the job with the error as `paused_reason`:
  `Cron execution unverified; reconcile <journal>: …` (often wrapping
  `unknown_execution: cron admission was not replayed`), `Canonical cron unknown; no automatic
  re-execution` or `cron admission identity conflict`. A later verified receipt may resume the job, but
  only if that exact pause (same `paused_at` and `paused_reason`) is still in place; a one-shot past its
  time stays paused. Before resuming by hand, check `hermes cron runs <id>`.
- **`/cron` in chats and the TUI (0.22.4+).** Bare `/cron` and `/cron list [--all]` read; every change
  runs on the gateway that owns the store, and a viewer connection (Ink TUI, Desktop, ACP) is refused
  with the reason. A message typed into a running cron session is refused with the reason too.
- **Parked bot-chat delivery (0.22.4+).** When the gateway of a `bot-chat:<profile>` target is down or
  starting, the delivery is parked in `cron/bot_chat_pending/` and handed over once it runs, but the run
  is booked as `delivery_failed` with `… queued (receipt …): target profile not running; admitted once
  it is`; the built-in `hermes cron doctor` reports it as not delivered. A record already `claimed` is
  never resent; a malformed one becomes `ambiguous`.
- **Webhook.** A route `platforms.webhook.extra.routes.<name>.cron_job: <id>` fires an existing job per
  event; the event body becomes transient context for that run.
- **Loop guardrails.** Unattended (cron, gateway) runs hard-stop repeated failing tool calls
  (`tool_loop_guardrails.hard_stop_after`): `exact_failure` 5 — the same call failing the same way,
  `same_tool_failure` 8 — one tool failing with any arguments, `idempotent_no_progress` 5 — the same
  read-only call repeated without new results. A sequence of different tools is not affected.
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
| `~/.hermes/cron/ticker_last_error` | `<epoch>` and the error of a failed tick; removed after the next good tick |
| `~/.hermes/cron/admissions/` | journals of fires handed to the gateway (0.22.4+); kept until reconciled — never edit |
| `~/.hermes/cron/bot_chat_pending/` | parked bot-chat deliveries (0.22.4+); kept as evidence |
| `~/.hermes/scripts/` | job scripts; anything outside is blocked |
| `~/.hermes/logs/agent.log` | scheduler and delivery logs |

A named profile keeps the same files under `~/.hermes/profiles/<name>/`. Since 0.22 one gateway per
machine serves all profiles and runs each profile's jobs with that profile's environment and secrets.
