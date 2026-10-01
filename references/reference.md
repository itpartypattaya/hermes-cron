# Справочник Hermes Cron

Этот файл описывает upstream Hermes Agent 0.21.5. Локальные GitOps-слои, чат-маршрутизация и
дополнительные поля должны документироваться отдельным profile-навыком, а не здесь. На другой
версии сверяй флаги с `hermes cron <команда> --help`.

## CLI

```bash
hermes cron list [--all]                  # без --all — только активные
hermes cron status                        # gateway, heartbeat, ближайший запуск
hermes cron doctor                        # встроенная проверка активных задач
hermes cron create "in 30m" "Напомни проверить резервную копию" [--name ...]
hermes cron edit <id> --schedule "0 9 * * 1-5"
hermes cron pause <id>
hermes cron resume <id>                   # повторяющаяся или разовая на паузе
hermes cron resume <id> --at <ISO>        # повторно поставить ОТРАБОТАВШУЮ разовую (только once)
hermes cron resume <id> --run-now         # то же, на сейчас
hermes cron run <id>                      # реальный запуск на ближайшем тике
hermes cron runs [<id>] [--limit 20]      # durable-история попыток (alias: history)
hermes cron incidents [--state alerted]   # инциденты сбоев
hermes cron incidents ack <incident_id>   # заглушить сигнатуру ошибки
hermes cron notepad <id> [list|get|set|delete] [ключ] [значение]
hermes cron remove <id>                   # alias: rm, delete
hermes cron tick                          # исполнить все due-задачи и выйти — не dry-run
```

Флаги `create` (у `edit` те же, пустая строка очищает поле):

| Флаг | Смысл |
| --- | --- |
| `--name` | человекочитаемое имя |
| `--deliver` | `origin`, `local`, платформа, `platform:chat_id[:thread_id]`, `bot-chat[:profile]` |
| `--failure-deliver` | адрес только для сообщений о сбоях; `local` их глушит |
| `--repeat N` | предел повторений |
| `--skill` | подключить навык (повторяемый); у `edit` ещё `--add-skill`, `--remove-skill`, `--clear-skills` |
| `--script` | скрипт из `~/.hermes/scripts/`: прекчек или, с `--no-agent`, сама задача |
| `--no-agent` | без LLM: stdout скрипта доставляется дословно; у `edit` обратное — `--agent` |
| `--monitor-script` / `--monitor-url` | монитор изменений; взаимоисключающие, несовместимы с `--no-agent` |
| `--continuity` | каждый запуск видит свой прошлый ответ; у `edit` — `--no-continuity` |
| `--workdir` | рабочий каталог: его `AGENTS.md` в промпт, cwd для инструментов |
| `--model`, `--provider` | закреп модели (инструмент агента это поле не ставит) |
| `--pin` | закрепить текущую основную модель; у `edit` — `--unpin` |
| `--reasoning-effort` | `none`…`ultra` на задачу |
| `--interpreter` | Python из своего venv для `.py`-скрипта |
| `--paused`, `--paused-reason` | создать выключенной одной записью, с причиной |

## Инструмент `cronjob_manage`

Действия: `create`, `list`, `update`, `pause`, `resume`, `remove`, `run`. Перед
`update`/`pause`/`resume`/`remove`/`run` — `list`, id не угадывать. `run` идёт в фоне и
возвращает handle; `prompt` при `run` — разовый контекст этого запуска, не сохраняется.
Поля `create`/`update`: `prompt`, `schedule`, `name`, `repeat`, `deliver`, `failure_deliver`,
`skills`, `script`, `monitor` (URL или путь скрипта), `no_agent`, `context_from`,
`continuity`, `enabled_toolsets`, `workdir`, `attach_to_session`, `pinned`; только при
create — `paused`, `paused_reason`. На `update` пустой список или строка очищает поле.
`model`, `provider`, `reasoning_effort`, `interpreter` — только через CLI.

## Расписания

| Назначение | Форма | Тип |
| --- | --- | --- |
| Один раз через 30 минут | `in 30m`, `in 2h`, `in 1d` | `once` |
| Повторять каждые 30 минут | `30m`, `every 30m`, `every hour` | `interval` |
| По дням и времени словами | `every monday 9am`, `weekdays at 9am`, `every day at 9am` | `cron` |
| Cron-выражение | `0 9 * * 1-5`, допустимы `MON-FRI`, `JAN-DEC` | `cron` |
| Один раз по точному времени | `2026-10-01T09:00:00+07:00` | `once` |

Время без зоны и cron-выражения считаются в `timezone` из `config.yaml`. Разовая задача на
время, прошедшее больше чем на пару минут, отклоняется. После создания сверь вычисленный
`next_run_at` с ожидаемым временем.

## Основные поля задачи

| Поле | Назначение |
| --- | --- |
| `id`, `name` | идентификация |
| `prompt` | самодостаточная инструкция новой агент-сессии |
| `schedule`, `schedule_display` | нормализованное расписание и его подпись |
| `enabled`, `state`, `paused_at`, `paused_reason` | разрешение запуска и маркеры паузы |
| `next_run_at`, `last_run_at`, `last_status`, `last_error` | runtime-состояние |
| `last_delivery_error`, `last_delivery_unverified` | доставка не удалась / не подтверждена адаптером |
| `last_dispatch`, `last_fire_error` | опоздавший или догоняющий запуск, ошибка постановки |
| `failure_streak` | неудачных запусков подряд (сбои доставки не считаются) |
| `deliver`, `failure_deliver`, `origin` | куда идёт ответ, куда сбои, где задачу создали |
| `skills`, `enabled_toolsets` | навыки и инструменты агента |
| `script`, `no_agent`, `monitor_script`, `monitor_url`, `monitor_state` | режимы без лишнего LLM |
| `context_from` | id задач, чей последний ответ подмешивается (своя `continuity` — тоже здесь) |
| `model`, `provider`, `base_url` | закреп маршрута; пусто — основная модель на момент запуска |
| `workdir`, `repeat` | рабочий каталог, предел повторений |
| `fire_claim`, `run_claim` | лизы планировщика — кто взял запуск; руками не трогать |

Запускается задача, если `enabled=true` и нет маркера паузы (`state=paused` или
`paused_at`). `origin` — место создания, а не автоматически правильный адресат.

## Статусы

`last_status`: `ok`; `error` — запуск упал; `delivery_failed` — запуск успешен, доставка нет;
`delivery_queued` — ответ в очереди доставки; `blocked_config` — предпроверка: нет ключа,
навыка или платформы, LLM не вызывался; `held` — удержано до восстановления квоты
провайдера; `interrupted` — прерван остановкой gateway.

`state`: `scheduled`, `paused`, `completed` (отработавшая разовая), `error` (не удалось
вычислить следующий запуск — читай `last_error`).

`executions.status`: `claimed`, `running`, `completed`, `failed`, `unknown`.
`executions.delivery_outcome`: `delivered`, `failed`, `not_configured`, `queued`,
`suppressed` (тишина или `local`), `suppressed_acked` (инцидент заглушён).

Инциденты (`cron_incidents`): `detected` → `alerted` → `resolved`/`closed`. Та же задача с той
же ошибкой — тот же инцидент; успешный запуск закрывает открытые.

## Маркеры в выводе

| Маркер | Где | Эффект |
| --- | --- | --- |
| пустой stdout | `no_agent` | тишина |
| последняя строка `{"wakeAgent": false}` | скрипт задачи | тишина, агент не будится |
| `[SILENT]` — весь ответ | LLM-задача | доставка подавлена; не переводить, не смешивать с текстом |
| `[CRON_FAILURE]` первой строкой | LLM-задача | запуск записывается как неудачный, текст — доказательство |

## Настройки `cron:` в config.yaml

| Ключ | По умолчанию | Смысл |
| --- | --- | --- |
| `catch_up_missed` | `true` | после простоя один догоняющий запуск; `false` — пропуск сверх окна |
| `allow_agent_scheduling` | `false` | дать cron-сессиям тулсет `cronjob` |
| `preflight` | `true` | проверить ключ, навыки и доставку до запуска → `blocked_config` |
| `model`, `model_provider` | `""` | модель для всех незакреплённых задач вместо основной |
| `wrap_response` | `true` | шапка с именем задачи и подвал у доставленного ответа |
| `delivery.notify` | `true` | доставка со звуком (Telegram) |
| `mirror_delivery` | `false` | ответ на сводку с её контекстом для всех задач |
| `max_parallel_jobs` | не ограничено | сколько due-задач параллельно за тик |
| `output_retention` | `50` | сколько output-файлов хранить на задачу |
| `script_timeout_seconds` | `3600` | таймаут скрипта `no_agent` |
| `failure_repeat_alert_hours` | `6` | через сколько повторить алерт о той же ошибке; `0` — каждый раз |

Иерархия модели: закреп задачи → `cron.model` → основная модель (`model.default`).
Закреплённая задача не идёт в общий `fallback_providers`.

## Прочие механизмы

- **Удержание по квоте.** Провайдер ответил «повтори через N с», и вся цепочка недоступна →
  `next_run_at` паркуется до восстановления окна, `last_status=held`.
- **Повтор при недоступной модели.** Сетевая ошибка до первого вызова модели → повтор через
  5, 15 и 30 минут, потом до следующего слота.
- **Защита gateway.** Задача, которая перезапускает или останавливает gateway, отклоняется
  при создании.
- **Webhook.** Маршрут `platforms.webhook.extra.routes.<имя>.cron_job: <id>` запускает
  существующую задачу по событию; тело события — разовый контекст запуска.
- **Самоудаление.** Если запуск удалил собственную задачу инструментом, финальный ответ всё
  равно доставляется. Из cron-сессии это возможно только при `allow_agent_scheduling: true`.

## Файлы runtime

| Путь | Содержимое |
| --- | --- |
| `~/.hermes/cron/jobs.json` | задачи и их runtime-состояние |
| `~/.hermes/cron/executions.db` | история попыток (`executions`) и инциденты (`cron_incidents`) |
| `~/.hermes/cron/deliveries.db` | очередь доставки через живой gateway |
| `~/.hermes/cron/notepad.db` | заметки задач |
| `~/.hermes/cron/output/<job_id>/` | сохранённый output запусков |
| `~/.hermes/cron/ticker_heartbeat` | `<epoch> <pid>` последнего тика; PID — процесс-писатель |
| `~/.hermes/cron/ticker_last_success` | время последнего успешного тика |
| `~/.hermes/scripts/` | скрипты задач; вне этого каталога запуск блокируется |
| `~/.hermes/logs/agent.log` | логи планировщика и доставки |
