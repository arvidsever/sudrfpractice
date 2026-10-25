#!/usr/bin/env python3
"""Непрерывный сбор КАД: 6 ч работы, 3 ч паузы, по кругу.

В отличие от таймера с фиксированным расписанием, время старта сдвигается
от цикла к циклу (6 + 3 = 9 ч не кратно 24), поэтому картина запросов
не повторяется сутки в сутки — это снижает шанс бана по объёму.
"""

from __future__ import annotations

import signal
import subprocess
import sys
import time

PYTHON = "/srv/sudrfpractice/.venv/bin/python"
RUN_HOURS = "6"
PAUSE_SECONDS = 3 * 3600


def _stop(signum, frame):
    global child
    if child is not None and child.poll() is None:
        child.terminate()
    sys.exit(0)


child = None
signal.signal(signal.SIGTERM, _stop)
signal.signal(signal.SIGINT, _stop)

while True:
    child = subprocess.Popen([PYTHON, "-m", "harvester", "kad", "--max-hours", RUN_HOURS])
    child.wait()
    child = None
    time.sleep(PAUSE_SECONDS)
