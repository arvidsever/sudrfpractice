#!/bin/bash
# Ночной прогон очереди. Запускается launchd в 01:00.
#
# Ночное окно проверяет сам харвестер: без флага --pilot обход работает
# только с 01:00 до 07:00 и на рассвете сам останавливается, вернув
# текущие окна в очередь. Поэтому скрипт можно запускать и вручную —
# днём он просто ничего не сделает.
#
# caffeinate держит Mac бодрствующим, пока идёт обход: ноутбук, уснувший
# в три часа ночи, оставил бы очередь недособранной без всякой ошибки.

set -u
cd "$(dirname "$0")/.." || exit 1

export PATH="/opt/homebrew/opt/postgresql@18/bin:/opt/homebrew/bin:$PATH"
export HARVESTER_MAX_RETRIES="${HARVESTER_MAX_RETRIES:-5}"

LOG="logs/night-$(date +%Y-%m-%d).log"
{
  echo "=== старт $(date '+%F %T') ==="
  caffeinate -i .venv/bin/python -m harvester run
  echo "=== стоп  $(date '+%F %T') ==="
  .venv/bin/python -m harvester queue
} >> "$LOG" 2>&1
