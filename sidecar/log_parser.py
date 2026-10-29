"""Parse ALAS (AzurLaneAutoScript) log output into a RunState.

Markers were verified by reading the ALAS source (read-only):
  - app start:          module/handler/login.py:159   logger.hr('App start')
  - login ok:           module/handler/login.py:61    logger.info('Login success')
  - commission scan:    module/commission/commission.py
                        logger.hr('Commission scan', level=1)
                        logger.hr uppercases the title, so the log shows
                        "COMMISSION SCAN" (box line plus an INFO line). Matched
                        case-sensitively. (The old "Commission detect" text is not
                        in the real log.) Field name stays commission_started.
  - commission DONE:    alas.py:592
                        logger.info(f'Scheduler: End task `{task}`')
                        with task == 'Commission' (module/config/argument/task.yaml:200;
                        dispatched by alas.py:188 `def commission`). Logged after the
                        task returns, whether or not it succeeded; failures show up as
                        entries in RunState.errors.
  - app already running: alas.py:177 (goto_main)
                        logger.info('App is already running, goto main page')
                        Logged when the game is still alive in the device session;
                        ALAS skips app start and login entirely. Counts as both
                        app started and login ok.

Regexes (not exact equality) are used because the logger wraps `hr` lines in
box characters and prefixes lines with timestamps.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_APP_START = re.compile(r"App start")
_APP_ALREADY_RUNNING = re.compile(r"App is already running")
_LOGIN_OK = re.compile(r"Login success")
_COMMISSION_STARTED = re.compile(r"COMMISSION SCAN")
_COMMISSION_DONE = re.compile(r"Scheduler:\s*End task\s*`Commission`")
_ERROR = re.compile(
    r"GameStuckError|GameNotRunningError|GameTooManyClickError|Traceback|\bCRITICAL\b"
)


@dataclass
class RunState:
    app_started: bool = False
    login_ok: bool = False
    commission_started: bool = False
    commission_done: bool = False
    errors: list[str] = field(default_factory=list)


def parse_lines(lines: Iterable[str]) -> RunState:
    state = RunState()
    for raw in lines:
        line = raw.rstrip("\r\n")
        if _APP_START.search(line):
            state.app_started = True
        if _APP_ALREADY_RUNNING.search(line):
            # No fresh app start or login happens on this path; reaching the
            # main page means the game is up and already logged in.
            state.app_started = True
            state.login_ok = True
        if _LOGIN_OK.search(line):
            state.login_ok = True
        if _COMMISSION_STARTED.search(line):
            state.commission_started = True
        if _COMMISSION_DONE.search(line):
            state.commission_done = True
        if _ERROR.search(line):
            state.errors.append(line.strip())
    return state


def newest_log_file(log_dir: Path) -> Path | None:
    """Return the `*.txt` file in log_dir with the largest mtime, or None."""
    files = [p for p in Path(log_dir).glob("*.txt") if p.is_file()]
    if not files:
        return None
    return max(files, key=lambda p: p.stat().st_mtime)
