# Справочник Hermes Cron

Этот файл описывает upstream Hermes Agent. Локальные GitOps-слои, чат-маршрутизация и
дополнительные поля должны документироваться отдельным profile-навыком, а не здесь.

## CLI

```bash
hermes cron list --all
hermes cron status
hermes cron create "in 30m" "Напомни проверить резервную копию"
hermes cron edit <id> --schedule "0 9 * * 1-5"
hermes cron pause <id>
hermes cron resume <id>
hermes cron run <id>
hermes cron runs <id> --limit 20
hermes cron remove <id>
```

`run` планирует настоящее выполнение на следующем tick. `tick` исполняет все due-задачи
по-настоящему; это не dry-run.

## Расписания

| Назначение | Форма | Тип |
| --- | --- | --- |
| Один раз через 30 минут | `in 30m` | `once` |
| Повторять каждые 30 минут | `30m` или `every 30m` | `interval` |
| По cron | `0 9 * * 1-5` | `cron` |
| Один раз по точному времени | `2026-10-01T09:00:00+07:00` | `once` |

Время cron-выражения интерпретируется в `timezone` из `config.yaml`. При сомнении
сравни вычисленный `next_run_at` с ожидаемым временем до того, как подтвердить задачу.

## Основные поля задачи

| Поле | Назначение |
| --- | --- |
| `id`, `name` | идентификация для управления и диагностики |
| `prompt` | самодостаточная инструкция новой агент-сессии |
| `schedule` | нормализованный тип и параметры расписания |
| `enabled`, `state` | разрешение выполнения и runtime-состояние |
| `next_run_at`, `last_run_at`, `last_status`, `last_error` | наблюдаемое runtime-состояние |
| `deliver` | `origin`, `local` или явный platform target; поддерживаемые формы зависят от адаптера |
| `skills`, `enabled_toolsets` | ограничение загружаемых навыков и инструментов |
| `script`, `no_agent` | precheck либо script-only выполнение |
| `context_from` | последний завершённый output другой задачи |
| `repeat` | предел повторений, если он задан |

`origin` — место создания задачи, а не автоматически правильный адресат. Явная доставка
должна включать thread/topic, если это требуется используемой платформой.

## Script-only и precheck

- `no_agent: true` + `script`: stdout скрипта — финальный output; пустой stdout означает
  тишину, ненулевой exit status означает отказ.
- `script` без `no_agent`: stdout добавляется в контекст агента. Последняя JSON-строка
  `{"wakeAgent": false}` позволяет пропустить вызов модели.
- Ограничивай output прекчека: он входит в контекст и влияет на стоимость/надёжность.

## Состояния и история

- `scheduled`: ждёт запуска.
- `paused`: должна быть также выключена через `enabled=false`.
- `error`: scheduler не смог штатно рассчитать или выполнить задачу; читай `last_error`.
- `completed`: одноразовая задача завершилась.

История попыток и output запуска являются более сильным доказательством, чем одно поле
`last_status`: другой writer может перезаписать запись задачи после выполнения.

## Файлы runtime

| Путь | Содержимое |
| --- | --- |
| `~/.hermes/cron/jobs.json` | задачи и их runtime-состояние |
| `~/.hermes/cron/executions.db` | durable-история попыток |
| `~/.hermes/cron/output/<job_id>/` | сохранённый output запусков |
| `~/.hermes/cron/ticker_heartbeat` | время последнего тика |
| `~/.hermes/cron/ticker_last_success` | время последнего успешного тика |
| `~/.hermes/logs/agent.log` | scheduler и delivery-логи |
