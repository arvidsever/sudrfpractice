#!/bin/bash
# Прогон очереди. Запускается launchd и поднимается заново, когда
# предыдущий заход закончился: суды уходят на паузу, окна кончаются,
# процесс выходит — а очередь остаётся.
#
# Окно обхода проверяет сам харвестер (`night_window`). С 20.08.2026 оно
# круглосуточное; когда было ночным, дневной запуск просто ничего не делал.
#
# caffeinate держит Mac бодрствующим, пока идёт обход: ноутбук, уснувший
# в три часа ночи, оставил бы очередь недособранной без всякой ошибки.

set -u
cd "$(dirname "$0")/.." || exit 1

export PATH="/opt/homebrew/opt/postgresql@18/bin:/opt/homebrew/bin:$PATH"
export HARVESTER_MAX_RETRIES="${HARVESTER_MAX_RETRIES:-5}"

LOG="logs/night-$(date +%Y-%m-%d).log"  # имя историческое: прогон уже не только ночной
{
  echo "=== старт $(date '+%F %T') ==="
  caffeinate -i .venv/bin/python -m harvester run
  echo "=== стоп  $(date '+%F %T') ==="
  .venv/bin/python -m harvester queue
} >> "$LOG" 2>&1
