# Hermes Cron Skill

A specialized skill for managing, configuring, and troubleshooting cron jobs (scheduled tasks) in **Hermes Agent**.

This repository contains the complete structured skill ready to be loaded by Hermes Agent or explored as a reference configuration.

---

## 🇷🇺 Русское описание

Специализированный скилл для создания, изменения, приостановки и диагностики cron-задач (задач по расписанию) на платформе **Hermes Agent**.

### Состав проекта

- **`SKILL.md`** — основной файл описания скилла с YAML-метаданными для Hermes. Содержит правила работы с планировщиком, разницу между типами отключения задач (пауза, выключение, удаление) и регламент документирования самоправок.
- **`scripts/cron-doctor.py`** — скрипт диагностики (`cron-doctor`). Проверяет активность планировщика, расписания, находит аномалии (например, расхождения `enabled/state`) и дрейф рантайма относительно одобренного Git-состояния.
- **`references/reference.md`** — справочник по всем полям задач, форматам расписаний и целям доставки сообщений.
- **`references/troubleshooting.md`** — разбор частых сбоев, рунбук по лечению проблем и подробная хроника реальных инцидентов.

---

## 🇺🇸 English Description

A production-grade Hermes Agent skill designed to manage the lifecycle of scheduled cron tasks (reminders, monitoring watchdogs, daily digests) and prevent typical scheduler pitfalls.

### Project Structure

- **`SKILL.md`** — the core skill definition file with frontmatter metadata. Houses the operational rules, auto-merge details, and instructions for logging automated self-edits.
- **`scripts/cron-doctor.py`** — diagnostic CLI tool (`cron-doctor`). Analyzes live `jobs.json` state, identifies runtime anomalies (e.g., mismatched enabled state vs paused scheduler state), and detects drift from the Git-tracked baseline.
- **`references/reference.md`** — complete schema definition for job fields, schedule formats, and messaging delivery targets.
- **`references/troubleshooting.md`** — comprehensive runbook detailing common failure modes and resolution paths.

---

## Installation / Установка

### 1. In Hermes Agent (as a Skill)

To load this skill into your Hermes Agent instance, clone this repository directly to your profile's `skills` folder:

```bash
git clone https://github.com/itpartypattaya/hermes-cron.git ~/.hermes/skills/hermes-cron
```

### 2. Manual Diagnostics (Cron Doctor)

To run the diagnostics tool manually on your server:

```bash
python3 ~/.hermes/skills/hermes-cron/scripts/cron-doctor.py
```

*For a detailed breakdown of a single job:*
```bash
python3 ~/.hermes/skills/hermes-cron/scripts/cron-doctor.py --job <job_id>
```
