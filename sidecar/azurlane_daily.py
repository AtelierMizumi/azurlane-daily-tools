"""Orchestrator core: run the ALAS Commission task once and report the outcome.

Order of steps: preflight (before start: Waydroid, container exists, host adb
port) -> container not already running -> ALAS config check (alas.json and
deploy.yaml Run; optionally applied) -> `docker start` -> resolution check via
the container's adb -> wait for WebUI -> watch the ALAS log until Commission
finishes or the time budget runs out -> `docker stop` (always, if we started
the container) -> ntfy and runs/<stamp>/summary.json.

All time and I/O goes through injected callables (runner, http_get, clock,
sleep) so the whole flow can be tested without sleeping.
"""
from __future__ import annotations

import argparse
import http.client
import json
import sys
import time
import urllib.request
from datetime import datetime
from pathlib import Path
from typing import Callable

import yaml

from alas_config import (apply_alas_config, apply_deploy_run, check_alas_config,
                         check_deploy_run, commission_next_run_warning, load_alas_config)
from daily_config import DailyConfig, load_daily_config
from log_parser import RunState, parse_lines
from preflight import Runner, SubprocessRunner, check_resolution, check_waydroid

WEBUI_WAIT_SECONDS = 120
WEBUI_POLL_SECONDS = 5
LOG_POLL_SECONDS = 30
DOCKER_TIMEOUT = 60
NTFY_TIMEOUT = 10
RUNS_DIR = Path(__file__).parent / "runs"

Stage = tuple[str, bool, str]

NOT_REACHED = "not reached"
# The e2e report always lists these, in this order (Ruling 35). Other stages
# (container state, resolution, ...) are slotted in after the stage they follow.
CANONICAL_STAGES = ["preflight", "config", "docker start", "webui", "app started",
                    "login ok", "commission started", "commission done",
                    "summary written", "docker stop"]


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
    deploy_path: Path | None = None,
    runs_dir: Path | None = None,
) -> dict:
    """Run Commission once. deploy_path=None skips the deploy.yaml Run check."""
    out = _run_core(cfg, runner, http_get, clock, sleep, alas_config_path, log_dir,
                    apply, deploy_path)
    return _finalize(out, cfg, Path(runs_dir) if runs_dir is not None else RUNS_DIR)


def _run_core(
    cfg: DailyConfig,
    runner: Runner,
    http_get: Callable[[str], int],
    clock: Callable[[], float],
    sleep: Callable[[float], None],
    alas_config_path: Path,
    log_dir: Path,
    apply: bool,
    deploy_path: Path | None,
) -> dict:
    stages: list[Stage] = []
    started = _now()

    def finish(result: str) -> dict:
        return {"result": result, "stages": stages, "started": started, "finished": _now()}

    container = cfg.alas_container

    # 1. Preflight BEFORE start (Waydroid session/FROZEN, container exists, host
    #    adb port). The resolution check needs the container up: it runs after start.
    problems = check_waydroid(runner, container, cfg.device)
    stages.append(("preflight", not problems, "; ".join(problems) or "ok"))
    if problems:
        return finish("preflight_failed")

    # 2. Container must not be running yet. Checked before any config write so
    #    the config is never touched while ALAS is up.
    rc, out = runner.run(
        ["docker", "inspect", "--type", "container", "-f", "{{.State.Running}}", container],
        DOCKER_TIMEOUT,
    )
    if rc != 0:
        stages.append(("container state", False, f"docker inspect failed: {out.strip()}"))
        return finish("preflight_failed")
    if out.strip() == "true":
        stages.append(("container state", False, f"'{container}' is already running; left untouched"))
        return finish("already_running")
    stages.append(("container state", True, "stopped"))

    # 3. ALAS config check (alas.json and deploy.yaml Run) and optional fix.
    next_run_warning: str | None = None
    try:
        data = load_alas_config(alas_config_path)
        alas_problems = check_alas_config(data)
        deploy_problems = check_deploy_run(deploy_path) if deploy_path is not None else []
        problems = alas_problems + deploy_problems
        if problems and apply:
            # Apply BEFORE `docker start`: never rewrite the config while the container runs.
            backups = []
            if alas_problems:
                backups.append(apply_alas_config(alas_config_path))
            if deploy_problems:
                backups.append(apply_deploy_run(deploy_path))
            data = load_alas_config(alas_config_path)
            problems = check_alas_config(data, deploy_path)
            stages.append(("config apply", not problems,
                           "backup at " + ", ".join(str(b) for b in backups)))
        next_run_warning = commission_next_run_warning(data)
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

        # 4b. Screen resolution via the container's adb (needs the container running).
        problems = check_resolution(runner, container, cfg.device)
        want_w, want_h = cfg.device.resolution
        stages.append(("resolution", not problems, "; ".join(problems) or f"{want_w}x{want_h}"))
        if problems:
            result = "preflight_failed"
            return finish(result)

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

        for name, reached in (("app started", state.app_started),
                              ("login ok", state.login_ok),
                              ("commission started", state.commission_started),
                              ("commission done", state.commission_done)):
            stages.append((name, reached, "seen in log" if reached else NOT_REACHED))

        # Ruling 46: success needs the commission scan to have started AND the task
        # to have ended AND no errors. "End task" alone is logged even when ALAS
        # only queued a Restart because the game was not running.
        if state.errors:
            result = "alas_error"
            detail = "; ".join(state.errors[:3])
            if state.commission_done:
                detail = "Commission ended with errors: " + detail
        elif state.commission_done and state.commission_started:
            result = "success"
            detail = "Commission finished"
        elif state.commission_done:
            result = "no_commission"
            detail = ("Commission ended without starting its commission scan "
                      "(game not running?)")
        else:
            result = "timeout"
            detail = f"Commission not finished within {cfg.run_minutes} min"
            if next_run_warning:
                detail += f"; {next_run_warning}"
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


# --------------------------------------------------------------------------
# Wrap-up: ntfy, summary.json, stage ordering (Rulings 34, 35)
# --------------------------------------------------------------------------

def _post_ntfy(url: str, text: str) -> None:
    req = urllib.request.Request(url, data=text.encode("utf-8"), method="POST")
    with urllib.request.urlopen(req, timeout=NTFY_TIMEOUT):
        pass


def _notify(url: str, out: dict) -> Stage:
    failed = [n for n, ok, d in out["stages"] if not ok and d != NOT_REACHED]
    text = f"Azur Lane daily: {out['result']}" + (f" (failed: {', '.join(failed)})" if failed else "")
    try:
        _post_ntfy(url, text)
    except (OSError, ValueError, http.client.HTTPException) as e:
        return ("ntfy", False, f"{type(e).__name__}: {e}")
    return ("ntfy", True, text)


def _write_summary(out: dict, cfg: DailyConfig, runs_dir: Path) -> Stage:
    try:
        target = Path(runs_dir) / datetime.now().strftime("%Y%m%d-%H%M%S")
        target.mkdir(parents=True, exist_ok=True)
        payload = {
            "result": out["result"],
            "stages": [{"name": n, "ok": ok, "detail": d} for n, ok, d in out["stages"]],
            "started": out["started"],
            "finished": out["finished"],
            "serial": cfg.device.serial,
        }
        path = target / "summary.json"
        path.write_text(json.dumps(payload, indent=2, ensure_ascii=False), encoding="utf-8")
    except OSError as e:
        return ("summary written", False, f"{type(e).__name__}: {e}")
    return ("summary written", True, str(path))


def _ordered(stages: list[Stage]) -> list[Stage]:
    """Canonical stages in canonical order (missing ones "not reached"); other
    stages stay right after the stage that preceded them."""
    ranked: list[tuple[float, int, Stage]] = []
    present: set[str] = set()
    last = -1.0
    for seq, stage in enumerate(stages):
        if stage[0] in CANONICAL_STAGES:
            last = float(CANONICAL_STAGES.index(stage[0]))
            present.add(stage[0])
            ranked.append((last, seq, stage))
        else:
            ranked.append((last + 0.5, seq, stage))
    for i, name in enumerate(CANONICAL_STAGES):
        if name not in present:
            ranked.append((float(i), len(stages) + i, (name, False, NOT_REACHED)))
    return [stage for _, _, stage in sorted(ranked, key=lambda t: (t[0], t[1]))]


def _finalize(out: dict, cfg: DailyConfig, runs_dir: Path) -> dict:
    stages: list[Stage] = list(out["stages"])
    out["finished"] = _now()  # after docker stop
    out["stages"] = stages
    if cfg.ntfy_url:
        stages.append(_notify(cfg.ntfy_url, out))
    stages.append(_write_summary(out, cfg, runs_dir))
    out["stages"] = _ordered(stages)
    return out


# --------------------------------------------------------------------------
# CLI
# --------------------------------------------------------------------------

STOP_WARNING = "WARNING: ALAS container may still be running; stop it manually"


def format_e2e_report(stages: list[Stage]) -> str:
    """One line per stage: `PASS|FAIL  <name>  <detail>`."""
    return "\n".join(
        f"{'PASS' if ok else 'FAIL'}  {name}  {detail}" for name, ok, detail in stages
    )


def _stop_failed(stages: list[Stage]) -> bool:
    # "not reached" means we never started the container, so there is nothing to stop.
    return any(name == "docker stop" and not ok and detail != NOT_REACHED
               for name, ok, detail in stages)


def exit_code_for(out: dict) -> int:
    """0 only when the run succeeded AND the container was stopped cleanly."""
    if out["result"] == "success" and not _stop_failed(out["stages"]):
        return 0
    return 1


def parse_args(argv: list[str] | None) -> argparse.Namespace:
    p = argparse.ArgumentParser(description="Run the ALAS Commission task once.")
    p.add_argument("--config", default="daily.yaml", help="daily config file (default: daily.yaml)")
    p.add_argument("--check", action="store_true",
                   help="only run preflight and the ALAS config check; do not start the container")
    p.add_argument("--apply", action="store_true",
                   help="let the run fix alas.json and deploy.yaml `Run` (a .bak backup is written first)")
    p.add_argument("--e2e", action="store_true",
                   help="full run with the per-stage report (same as the default mode)")
    return p.parse_args(argv)


def _http_status(url: str) -> int:
    try:
        with urllib.request.urlopen(url, timeout=5) as resp:
            return resp.status
    except OSError:  # URLError, HTTPError, timeouts, connection refused
        return 0


def _real_deps() -> tuple[Runner, Callable[[str], int], Callable[[], float], Callable[[float], None]]:
    """The real runner/http/clock/sleep. Tests replace this function."""
    return SubprocessRunner(), _http_status, time.monotonic, time.sleep


def run_check(cfg: DailyConfig, runner: Runner, alas_config_path: Path,
              deploy_path: Path | None = None) -> list[Stage]:
    """Preflight + ALAS config check only. Never starts or stops anything."""
    stages: list[Stage] = []
    problems = check_waydroid(runner, cfg.alas_container, cfg.device)
    stages.append(("preflight", not problems, "; ".join(problems) or "ok"))

    # The resolution needs the container's adb, so it is only checked when the
    # container is already running (Ruling 31).
    if problems:
        stages.append(("resolution", True, "skipped (preflight failed)"))
    else:
        rc, out = runner.run(
            ["docker", "inspect", "--type", "container", "-f", "{{.State.Running}}",
             cfg.alas_container],
            DOCKER_TIMEOUT,
        )
        if rc != 0:
            stages.append(("resolution", True, f"skipped (docker inspect rc={rc})"))
        elif out.strip() != "true":
            stages.append(("resolution", True, "skipped (container stopped)"))
        else:
            res = check_resolution(runner, cfg.alas_container, cfg.device)
            want_w, want_h = cfg.device.resolution
            stages.append(("resolution", not res, "; ".join(res) or f"{want_w}x{want_h}"))

    try:
        data = load_alas_config(alas_config_path)
        problems = check_alas_config(data, deploy_path)
        stages.append(("config", not problems, "; ".join(problems) or "ok"))
        warning = commission_next_run_warning(data)
        if warning:  # Ruling 33: a warning, not a failure
            stages.append(("next run", True, warning))
    except (OSError, ValueError) as e:
        stages.append(("config", False, str(e)))
    return stages


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    try:
        cfg = load_daily_config(Path(args.config))
    except (OSError, ValueError, yaml.YAMLError) as e:
        print(f"FAIL  daily config  {e}", file=sys.stderr)
        return 1
    runner, http_get, clock, sleep = _real_deps()
    alas_config_path = Path(cfg.alas_repo_dir) / "config" / "alas.json"
    deploy_path = Path(cfg.alas_repo_dir) / "config" / "deploy.yaml"
    log_dir = Path(cfg.alas_repo_dir) / "log"

    if args.check:
        stages = run_check(cfg, runner, alas_config_path, deploy_path)
        print(format_e2e_report(stages))
        return 0 if all(ok for _, ok, _ in stages) else 1

    out = run_daily(
        cfg, runner, http_get, clock, sleep,
        alas_config_path=alas_config_path, log_dir=log_dir, apply=args.apply,
        deploy_path=deploy_path,
    )
    print(format_e2e_report(out["stages"]))
    print(f"RESULT: {out['result']}")
    if _stop_failed(out["stages"]):
        print(STOP_WARNING, file=sys.stderr)
    return exit_code_for(out)


if __name__ == "__main__":
    sys.exit(main())
