"""Parse ALAS (AzurLaneAutoScript) log output into a RunState.

Markers were verified by reading the ALAS source (read-only):
  - app start:          module/handler/login.py:159   logger.hr('App start')
  - login ok:           module/handler/login.py:61    logger.info('Login success')
  - commission started: module/commission/commission.py:70
                        logger.hr('Commission detect')
  - commission DONE:    alas.py:592
                        logger.info(f'Scheduler: End task `{task}`')
                        with task == 'Commission' (module/config/argument/task.yaml:200;
                        dispatched by alas.py:188 `def commission`). Logged after the
                        task returns, whether or not it succeeded; failures show up as
                        entries in RunState.errors.

Regexes (not exact equality) are used because the logger wraps `hr` lines in
box characters and prefixes lines with timestamps.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Iterable

_APP_START = re.compile(r"App start")
_LOGIN_OK = re.compile(r"Login success")
_COMMISSION_STARTED = re.compile(r"Commission detect")
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
