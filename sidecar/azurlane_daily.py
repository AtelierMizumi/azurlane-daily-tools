"""Orchestrator core: run the ALAS Commission task once and report the outcome.

Order of steps: preflight -> container not already running -> ALAS config
check (optionally applied) -> `docker start` -> wait for WebUI -> watch the
ALAS log until Commission finishes or the time budget runs out -> `docker stop`
(always, if we started the container).

All time and I/O goes through injected callables (runner, http_get, clock,
sleep) so the whole flow can be tested without sleeping.
"""
from __future__ import annotations

from datetime import datetime
from pathlib import Path
from typing import Callable

from alas_config import apply_alas_config, check_alas_config, load_alas_config
from daily_config import DailyConfig
from log_parser import RunState, parse_lines
from preflight import Runner, check_waydroid

WEBUI_WAIT_SECONDS = 120
WEBUI_POLL_SECONDS = 5
LOG_POLL_SECONDS = 30
DOCKER_TIMEOUT = 60

Stage = tuple[str, bool, str]


class _LogTail:
    """Yields only log lines written after the snapshot was taken.

    Existing `*.txt` files are read from their size at snapshot time; files
    that appear later are read from offset 0. Only complete lines are consumed.
    """

    def __init__(self, log_dir: Path):
        self.log_dir = Path(log_dir)
        self.offsets: dict[str, int] = {}
        # Files that could not be snapshotted existed before the run; their
        # content is unknown, so they are never fed to the parser (Ruling 11).
        self.ignored: set[str] = set()
        # True when the directory could not even be listed at snapshot time.
        self.snapshot_pending = False
        self._take_snapshot()

    def _list(self, snapshot: bool) -> list[Path] | None:
        """List `*.txt` files; None if the directory cannot be listed.

        While snapshotting, a file whose is_file() fails is added to `ignored`.
        """
        try:
            candidates = sorted(self.log_dir.glob("*.txt"))
        except OSError:
            return None
        files: list[Path] = []
        for p in candidates:
            try:
                if p.is_file():
                    files.append(p)
            except OSError:  # e.g. PermissionError from stat
                if snapshot:
                    self.ignored.add(p.name)
        return files

    def _take_snapshot(self) -> None:
        files = self._list(snapshot=True)
        if files is None:
            self.snapshot_pending = True  # redo at the first successful listing
            return
        self.snapshot_pending = False
        for p in files:
            try:
                self.offsets[p.name] = p.stat().st_size
            except OSError:  # vanished or unreadable between glob and stat
                self.ignored.add(p.name)

    def read_new(self) -> list[str]:
        lines: list[str] = []
        if self.snapshot_pending:
            # The start-time listing failed: treat whatever exists now as
            # pre-existing rather than replaying old lines.
            self._take_snapshot()
            return lines
        for p in self._list(snapshot=False) or []:
            if p.name in self.ignored:
                continue
            offset = self.offsets.get(p.name, 0)
            try:
                with open(p, "rb") as f:
                    f.seek(0, 2)
                    if f.tell() < offset:  # truncated or rotated in place
                        offset = 0
                    f.seek(offset)
                    data = f.read()
            except OSError:
                continue
            end = data.rfind(b"\n") + 1  # leave a partial last line for next poll
            self.offsets[p.name] = offset + end
            if end:
                lines.extend(data[:end].decode("utf-8", errors="replace").splitlines())
        return lines


def _now() -> str:
    return datetime.now().isoformat(timespec="seconds")


def run_daily(
    cfg: DailyConfig,
    runner: Runner,
    http_get: Callable[[str], int],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    alas_config_path: Path,
    log_dir: Path,
    apply: bool = False,
) -> dict:
    stages: list[Stage] = []
    started = _now()

    def finish(result: str) -> dict:
        return {"result": result, "stages": stages, "started": started, "finished": _now()}

    container = cfg.alas_container

    # 1. Preflight (Waydroid session, container exists, resolution).
    problems = check_waydroid(runner, container, cfg.device)
    stages.append(("preflight", not problems, "; ".join(problems) or "ok"))
    if problems:
        return finish("preflight_failed")

    # 2. Container must not be running yet. Checked before any config write so
    #    the config is never touched while ALAS is up.
    rc, out = runner.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container], DOCKER_TIMEOUT
    )
    if rc != 0:
        stages.append(("container state", False, f"docker inspect failed: {out.strip()}"))
        return finish("preflight_failed")
    if out.strip() == "true":
        stages.append(("container state", False, f"'{container}' is already running; left untouched"))
        return finish("already_running")
    stages.append(("container state", True, "stopped"))

    # 3. ALAS config check (and optional fix).
    try:
        problems = check_alas_config(load_alas_config(alas_config_path))
        if problems and apply:
            # Apply BEFORE `docker start`: never rewrite the config while the container runs.
            backup = apply_alas_config(alas_config_path)
            problems = check_alas_config(load_alas_config(alas_config_path))
            stages.append(("config apply", not problems, f"backup at {backup}"))
    except (OSError, ValueError) as e:  # FileNotFoundError, bad JSON
        stages.append(("config", False, str(e)))
        return finish("config_failed")
    stages.append(("config", not problems, "; ".join(problems) or "ok"))
    if problems:
        return finish("config_failed")

    # Snapshot existing logs so lines from earlier runs never reach the parser.
    tail = _LogTail(log_dir)

    # 4. Start the container; from here on `docker stop` is guaranteed.
    container_started = False
    result = "alas_error"
    try:
        container_started = True  # set first: a failed/partial start must still be stopped
        rc, out = runner.run(["docker", "start", container], DOCKER_TIMEOUT)
        stages.append(("docker start", rc == 0, out.strip() or ("ok" if rc == 0 else f"rc={rc}")))
        if rc != 0:
            return finish("alas_error")

        # 5. Wait for the WebUI.
        deadline = clock() + WEBUI_WAIT_SECONDS
        ready = False
        while True:
            try:
                ready = http_get(cfg.alas_webui_url) == 200
            except Exception:  # connection refused etc. just means "not up yet"
                ready = False
            if ready or clock() >= deadline:
                break
            sleep(WEBUI_POLL_SECONDS)
        stages.append(("webui", ready, cfg.alas_webui_url if ready else
                       f"no HTTP 200 from {cfg.alas_webui_url} within {WEBUI_WAIT_SECONDS}s"))
        if not ready:
            result = "webui_timeout"
            return finish(result)

        # 6. Watch the log (only lines written since the snapshot).
        deadline = clock() + cfg.run_minutes * 60
        seen: list[str] = []
        state = RunState()
        while True:
            seen.extend(tail.read_new())
            state = parse_lines(seen)
            if state.commission_done or clock() >= deadline:
                break
            sleep(LOG_POLL_SECONDS)

        if state.commission_done and not state.errors:
            result = "success"
            detail = "Commission finished"
        elif state.errors:
            result = "alas_error"
            detail = "; ".join(state.errors[:3])
            if state.commission_done:
                detail = "Commission ended with errors: " + detail
        else:
            result = "timeout"
            detail = f"Commission not finished within {cfg.run_minutes} min"
        stages.append(("commission", result == "success", detail))
        return finish(result)
    except Exception as e:  # report instead of crashing; `finally` still stops the container
        stages.append(("run", False, f"{type(e).__name__}: {e}"))
        return finish("alas_error")
    finally:
        if container_started:
            # Guarded: an exception here must not replace the returned dict.
            try:
                rc, out = runner.run(["docker", "stop", container], DOCKER_TIMEOUT)
                stages.append(("docker stop", rc == 0,
                               out.strip() or ("ok" if rc == 0 else f"rc={rc}")))
            except Exception as e:
                stages.append(("docker stop", False, f"{type(e).__name__}: {e}"))
