# Runbook: cron misbehaves

Do not edit the schedule until you know which layer broke.

## Order of checks

1. **Scheduler.** `cron-doctor.py` and `hermes cron status`: is the heartbeat fresh and is the gateway
   running? If the gateway does not tick, fixing one job will not help.
2. **Job record.** `enabled`, pause markers (`state=paused`, `paused_at`), schedule, `next_run_at`. A record
   with `enabled=true` and a pause marker does not run, although `cron list` shows it as scheduled.
3. **Execution attempt.** `hermes cron runs <id>` or `cron-doctor.py --job <id>`: did the scheduler try to
   run the job, and how did it end?
4. **Delivery.** The run's `delivery_outcome`, `last_delivery_error`, saved output and the log. A
   successful run does not prove that the delivery target is right.
5. **Other writers.** A deployment, a config sync or a human operator can rewrite `jobs.json` after your
   change.

## Symptoms

**The job did not fire.** Heartbeat first, then `next_run_at`, then `runs`. `state=error` needs
`last_error` read; do not resume it blindly. `last_status=blocked_config` means preflight found no provider
key, skill or delivery platform; no LLM call was made and one alert was sent. `last_status=held` means the
provider reported an exhausted quota and Hermes postponed the run until it recovers — not a bug.

**The job fired at the wrong time.** A catch-up after a gateway restart (`cron.catch_up_missed`) or after
`resume`: a slot missed while paused fires once. A re-run 5, 15 or 30 minutes after a network error is the
built-in retry for an unreachable model. Compare the run time with restarts and `resume`, not only with the
cron expression.

**The message arrived twice.** Usually the prompt asked the agent to "send" it and the scheduler delivered
the reply as well. Remove "send" from the prompt and keep one delivery path; use script-only for fixed text.

**The message went to the wrong place.** Expand `deliver: origin` into the actual chat/thread. Origin is
where the job was created, not its audience.

**Failure notices flood a shared chat.** Set `failure_deliver` (another target or `local`). A repeated
alert for the same error comes once per `cron.failure_repeat_alert_hours`; `hermes cron incidents ack <id>`
silences it for good.

**Silence instead of a watchdog alert.** Empty stdout of a script-only job, `wakeAgent=false` of a
pre-check, unchanged monitor output and a `[SILENT]` reply are all intended silence. Test the positive case
with an artificially triggered alert condition. A monitor whose output contains a timestamp, on the
contrary, wakes the agent on every tick.

**The script does not start.** "Blocked: script path resolves outside the scripts directory" — the script
must live in the profile's `~/.hermes/scripts/`. "Script not found" — every profile has its own scripts
directory.

**The job answered with another model or failed without fallback.** An unpinned job takes the main model at
fire time (or `cron.model`) — after `hermes model`, cron jobs change too. A pinned job (`model`/`provider`/
`base_url`) ignores that and does not use the global fallback: when its provider is down, the run fails or
is held.

**A removed or disabled job came back.** There is a persistent writer. Find the configuration source and
change it together with the runtime; removing the job from `jobs.json` alone is not durable.

**An LLM job "resumed" or "removed" other jobs, but nothing changed.** A cron session has no `cronjob`
toolset (`cron.allow_agent_scheduling: false`): the model answers in text without a tool call. Make such
changes from outside, or with a script-only job.

## What to attach to an incident report

- id, name and normalized schedule;
- expected and actual time/target;
- `enabled`, `state`, `next_run_at`, `last_status`, `last_error`;
- the `hermes cron runs` entry with `delivery_outcome` and the delivery log lines;
- open incidents (`hermes cron incidents`);
- gateway restart and config sync times, if any.
