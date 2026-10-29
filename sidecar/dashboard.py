#!/usr/bin/env python3
"""azdaily daemon — web dashboard + supervisor for azurlane-daily-tools.

One process, three jobs:
  1. Serve a small web UI (dashboard.html + JSON API) that shows what the
     tool is doing: live pipeline stages (read from runs/latest-state.json,
     written by the orchestrator itself), ALAS schedule, history, log tail,
     container/Waydroid/watchdog status.
  2. Supervise ALAS in "always-on" mode: keep the container running while
     the daemon runs and perform the watchdog checks in-process (reusing
     watchdog.py), so AL can run "whenever the computer is on" per the
     product goal.
  3. Run the run-once pipeline on demand (spawn `azurlane_daily.py` as a
     subprocess) or on a daily schedule when always_on is disabled.

Security model: bind 127.0.0.1 by default; binding a non-loopback address
requires a token, and every POST is checked against its Origin header.
No third-party dependencies beyond what the sidecar already uses.

Usage:
    ./azdaily daemon [--port N] [--host H] [--token T] [--no-browser]
"""
from __future__ import annotations

import argparse
import datetime as dt
import json
import os
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import urlparse

from alas_config import commission_next_run_warning, load_alas_config, mission_claim_note
from daily_config import DailyConfig, load_daily_config
from alas_report import build_report
from log_parser import newest_log_file
from watchdog import (Notifier, ScreenChecker, container_running, newest_log_age,
                      webui_alive)

SIDE = Path(__file__).resolve().parent
STATE_FILE = SIDE / "runs" / "latest-state.json"
RUNS_DIR = SIDE / "runs"
HTML_FILE = SIDE / "dashboard.html"
DOCKER_TIMEOUT = 20
CACHE_TTL = 5.0
SCHEDULE_TTL = 20.0
WAYDROID_TTL = 10.0
LOGTAIL_LINES = 50
HISTORY_LIMIT = 50


def _now_iso() -> str:
    return dt.datetime.now().isoformat(timespec="seconds")


def _run_cmd(cmd: list[str], timeout: int = DOCKER_TIMEOUT) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        return p.returncode, (p.stdout + p.stderr).strip()
    except (subprocess.TimeoutExpired, OSError) as e:
        return 1, f"{type(e).__name__}: {e}"


class Dashboard:
    """State + actions behind the HTTP API. All request-facing methods are
    safe to call from handler threads; mutable state is lock-guarded."""

    def __init__(self, cfg: DailyConfig, spawn=None, now=None,
                 screen_checker: ScreenChecker | None = None,
                 notifier: Notifier | None = None, version: str = "?",
                 config_file: Path | None = None):
        self.cfg = cfg
        self.version = version
        self._config_file = config_file or SIDE / "daily.yaml"
        self.spawn_fn = spawn or self._default_spawn
        self.now = now or time.time
        self.screen = screen_checker or ScreenChecker(
            cfg.alas_container, cfg.device.serial)
        self.notifier = notifier or Notifier(cfg.ntfy_url, threshold=2)
        self.shutdown = threading.Event()
        self._lock = threading.Lock()
        self._proc = None            # Popen of the pipeline subprocess
        self._proc_mode: str | None = None
        self._proc_output: deque[str] = deque(maxlen=200)
        self._proc_exit: int | None = None
        self._proc_finished: str | None = None
        self._container_cache: tuple[float, bool] = (0.0, False)
        self._waydroid_cache: tuple[float, str] = (0.0, "unknown")
        self._schedule_cache: tuple[float, dict] = (0.0, {})
        self._watchdog: dict = {"updated": None}
        self._last_run_date: str | None = None
        self._last_action: str | None = None

    # ------------------------------------------------------------ subprocess

    @staticmethod
    def _default_spawn(args: list[str]) -> subprocess.Popen:
        return subprocess.Popen(args, cwd=SIDE, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, text=True)

    def child_alive(self) -> bool:
        with self._lock:
            return self._proc is not None and self._proc.poll() is None

    def start_run(self, mode: str) -> tuple[bool, str]:
        """Spawn the orchestrator; returns (ok, message)."""
        if mode not in ("check", "run", "apply"):
            return False, f"unknown mode {mode!r}"
        with self._lock:
            if self._proc is not None and self._proc.poll() is None:
                return False, "a pipeline run is already active"
            args = [sys.executable, str(SIDE / "azurlane_daily.py"),
                    "--config", str(self._config_path())]
            if mode == "check":
                args.append("--check")
            else:
                args.append("--e2e")
                if mode == "apply":
                    args.append("--apply")
            self._proc = self.spawn_fn(args)
            self._proc_mode = mode
            self._proc_exit = None
            self._proc_finished = None
            self._proc_output.clear()
            threading.Thread(target=self._drain, daemon=True).start()
        return True, f"spawned {mode}"

    def _drain(self) -> None:
        proc = self._proc
        assert proc is not None and proc.stdout is not None
        for line in proc.stdout:
            self._proc_output.append(line.rstrip("\n"))
        with self._lock:
            self._proc_exit = proc.returncode
            self._proc_finished = _now_iso()

    def stop_run(self) -> tuple[bool, str]:
        with self._lock:
            proc, mode = self._proc, self._proc_mode
        if proc is None or proc.poll() is not None:
            return False, "no active pipeline run"
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
        rc, out = _run_cmd(["docker", "stop", self.cfg.alas_container])
        detail = f"terminated {mode} run; docker stop rc={rc} {out}".strip()
        self._mark_stopped(detail)
        return True, detail

    def _mark_stopped(self, detail: str) -> None:
        """Flag the state file so the UI shows why the run ended abruptly."""
        try:
            payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            payload = {"mode": self._proc_mode, "stages": [], "started": _now_iso()}
        payload["result"] = "stopped_by_user"
        payload["finished"] = _now_iso()
        payload["stop_detail"] = detail
        try:
            STATE_FILE.parent.mkdir(parents=True, exist_ok=True)
            tmp = STATE_FILE.with_suffix(".tmp")
            tmp.write_text(json.dumps(payload, indent=2, ensure_ascii=False),
                           encoding="utf-8")
            tmp.replace(STATE_FILE)
        except OSError:
            pass

    def _config_path(self) -> Path:
        """The daily.yaml this daemon was started with (kept for children)."""
        return Path(self._config_file).expanduser()  # set by main()

    # ------------------------------------------------------------- supervisor

    def supervise_once(self) -> None:
        """One watchdog pass + always-on enforcement. Runs on its own thread."""
        running = self.container_running()
        if not running:
            self.screen.last_frame = None
            self.screen.last_change = time.time()
            self.notifier.record_failure("container-down", "docker container not running")
            if self.cfg.daemon.always_on and not self.child_alive():
                rc, out = _run_cmd(["docker", "start", self.cfg.alas_container])
                self._last_action = (
                    f"always-on auto-start rc={rc} {out}".strip() + f" at {_now_iso()}")
        else:
            self.notifier.record_recovery("container-down")

        webui = webui_alive(self.cfg.alas_webui_url)
        if webui:
            self.notifier.record_recovery("webui-down")
        else:
            self.notifier.record_failure("webui-down",
                                         f"no HTTP 200 from {self.cfg.alas_webui_url}")

        age = newest_log_age(self.cfg.alas_repo_dir)
        if age is not None and age > 3600:
            self.notifier.record_failure("log-stale", f"newest ALAS log is {int(age)}s old")
        else:
            self.notifier.record_recovery("log-stale")

        stalled = self.screen.stalled() if running else False
        if stalled:
            self.notifier.record_failure("screen-stall",
                                         "game screen unchanged for a long time")
        else:
            self.notifier.record_recovery("screen-stall")

        self._watchdog = {
            "container": "up" if running else "down",
            "webui": "up" if webui else "down",
            "log_age_s": None if age is None else int(age),
            "screen": ("stalled" if stalled else
                       "watching" if running else "unknown (container down)"),
            "updated": _now_iso(),
            "last_action": self._last_action,
        }

    def _maybe_scheduled_run(self) -> None:
        """run_at fallback (only when always_on is off): one pipeline run/day."""
        if self.cfg.daemon.always_on or not self.cfg.daemon.run_at:
            return
        if self.child_alive():
            return
        now = dt.datetime.now()
        hh, mm = (int(x) for x in self.cfg.daemon.run_at.split(":"))
        due = now.replace(hour=hh, minute=mm, second=0, microsecond=0)
        if now < due:
            return
        today = now.date().isoformat()
        if self._last_run_date == today:
            return
        self._last_run_date = today
        self.start_run("run")

    def supervisor_loop(self) -> None:
        while not self.shutdown.is_set():
            try:
                self.supervise_once()
                self._maybe_scheduled_run()
            except Exception as e:  # the dashboard must survive a bad check
                print(f"dashboard supervisor error: {type(e).__name__}: {e}",
                      file=sys.stderr)
            self.shutdown.wait(self.cfg.daemon.watchdog_interval)

    # ------------------------------------------------------------- read models

    def container_running(self) -> bool:
        ts, cached = self._container_cache
        if self.now() - ts < CACHE_TTL:
            return cached
        rc, out = _run_cmd(["docker", "inspect", "--type", "container",
                            "-f", "{{.State.Running}}", self.cfg.alas_container])
        running = rc == 0 and out == "true"
        self._container_cache = (self.now(), running)
        return running

    def waydroid_session(self) -> str:
        ts, cached = self._waydroid_cache
        if self.now() - ts < WAYDROID_TTL:
            return cached
        rc, out = _run_cmd(["waydroid", "status"], timeout=10)
        session = "unknown"
        if rc == 0:
            m = [l for l in out.splitlines() if l.startswith("Session:")]
            if m:
                session = m[0].split(":", 1)[1].strip() or "unknown"
        self._waydroid_cache = (self.now(), session)
        return session

    def schedule(self) -> dict:
        ts, cached = self._schedule_cache
        if self.now() - ts < SCHEDULE_TTL and cached:
            return cached
        out: dict = {"tasks": [], "next_runs": {}, "anomalies": [],
                     "mission_note": None, "commission_warning": None}
        log = newest_log_file(self.cfg.alas_repo_dir / "log")
        if log is not None:
            try:
                report = build_report(log.read_text(encoding="utf-8", errors="replace"))
                out["tasks"] = report["tasks"][-20:]
                out["next_runs"] = report["next_runs"]
                out["anomalies"] = report["anomalies"][-5:]
                out["log_file"] = str(log)
            except OSError:
                pass
        try:
            data = load_alas_config(self.cfg.alas_repo_dir / "config" / "alas.json")
            out["mission_note"] = mission_claim_note(data)
            out["commission_warning"] = commission_next_run_warning(data)
        except (OSError, ValueError):
            pass
        self._schedule_cache = (self.now(), out)
        return out

    def pipeline_state(self) -> dict:
        payload: dict = {}
        try:
            payload = json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            pass
        with self._lock:
            active = self._proc is not None and self._proc.poll() is None
            return {
                "active": active,
                "mode": self._proc_mode if active else payload.get("mode"),
                "exit_code": None if active else self._proc_exit,
                "finished": self._proc_finished or payload.get("finished"),
                "output": list(self._proc_output)[-40:] if active else [],
                **{k: payload.get(k) for k in
                   ("result", "stages", "started", "stop_detail")},
            }

    def history(self) -> list[dict]:
        rows = []
        if not RUNS_DIR.is_dir():
            return rows
        for summary in sorted(RUNS_DIR.glob("*/summary.json"), reverse=True)[:HISTORY_LIMIT]:
            try:
                data = json.loads(summary.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            rows.append({
                "dir": summary.parent.name,
                "result": data.get("result"),
                "started": data.get("started"),
                "finished": data.get("finished"),
            })
        return rows

    def logtail(self) -> dict:
        log = newest_log_file(self.cfg.alas_repo_dir / "log")
        if log is None:
            return {"log_file": None, "lines": []}
        try:
            lines = log.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            return {"log_file": str(log), "lines": []}
        return {"log_file": str(log), "lines": lines[-LOGTAIL_LINES:]}

    def status(self) -> dict:
        return {
            "daemon": {"version": self.version, "pid": os.getpid(),
                       "listen": self.cfg.daemon.listen,
                       "always_on": self.cfg.daemon.always_on,
                       "run_at": self.cfg.daemon.run_at,
                       "watchdog": self.cfg.daemon.watchdog},
            "container": {"running": self.container_running(),
                          "name": self.cfg.alas_container},
            "waydroid": {"session": self.waydroid_session()},
            "watchdog": self._watchdog,
            "pipeline": self.pipeline_state(),
            "schedule": self.schedule(),
            "config": {"run_minutes": self.cfg.run_minutes,
                       "webui": self.cfg.alas_webui_url,
                       "repo_dir": str(self.cfg.alas_repo_dir)},
        }

    # ------------------------------------------------------------- container ops

    def container_action(self, action: str) -> tuple[bool, str]:
        if action not in ("start", "stop"):
            return False, f"unknown action {action!r}"
        if action == "stop" and self.child_alive():
            return False, "a pipeline run is active; stop it first"
        rc, out = _run_cmd(["docker", action, self.cfg.alas_container], timeout=60)
        self._last_action = f"manual {action} rc={rc} at {_now_iso()}"
        self._container_cache = (0.0, False)  # invalidate
        return rc == 0, out or f"rc={rc}"

    # ------------------------------------------------------------------ server

    def make_server(self, host: str, port: int) -> ThreadingHTTPServer:
        daemon = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, fmt, *args):  # silence per-request stderr
                pass

            def _authorized(self) -> bool:
                token = daemon.cfg.daemon.token
                if not token:
                    return True
                got = self.headers.get("X-Azdaily-Token") or ""
                auth = self.headers.get("Authorization") or ""
                if auth.startswith("Bearer "):
                    got = auth[len("Bearer "):]
                return got == token

            def _origin_ok(self) -> bool:
                origin = self.headers.get("Origin")
                if not origin:
                    return True  # non-browser client (curl, scripts)
                host = self.headers.get("Host", "")
                return urlparse(origin).netloc == host

            def _json(self, code: int, payload) -> None:
                body = json.dumps(payload, ensure_ascii=False).encode()
                self.send_response(code)
                self.send_header("Content-Type", "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._authorized():
                    return self._json(401, {"error": "token required"})
                path = self.path.split("?", 1)[0]
                if path == "/":
                    try:
                        body = HTML_FILE.read_bytes()
                    except OSError:
                        return self._json(500, {"error": "dashboard.html missing"})
                    self.send_response(200)
                    self.send_header("Content-Type", "text/html; charset=utf-8")
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)
                elif path == "/api/status":
                    self._json(200, daemon.status())
                elif path == "/api/history":
                    self._json(200, {"runs": daemon.history()})
                elif path == "/api/logtail":
                    self._json(200, daemon.logtail())
                else:
                    self._json(404, {"error": "not found"})

            def do_POST(self):
                if not self._authorized():
                    return self._json(401, {"error": "token required"})
                if not self._origin_ok():
                    return self._json(403, {"error": "cross-origin POST rejected"})
                path = self.path.split("?", 1)[0]
                try:
                    length = int(self.headers.get("Content-Length") or 0)
                    body = json.loads(self.rfile.read(length) or b"{}")
                except ValueError:
                    return self._json(400, {"error": "invalid JSON body"})
                if path == "/api/run":
                    ok, msg = daemon.start_run(str(body.get("mode", "run")))
                    return self._json(200 if ok else 409, {"ok": ok, "detail": msg})
                if path == "/api/stop":
                    ok, msg = daemon.stop_run()
                    return self._json(200 if ok else 409, {"ok": ok, "detail": msg})
                if path == "/api/container":
                    ok, msg = daemon.container_action(str(body.get("action", "")))
                    return self._json(200 if ok else 409, {"ok": ok, "detail": msg})
                self._json(404, {"error": "not found"})

        server = ThreadingHTTPServer((host, port), Handler)
        server.daemon_threads = True
        return server


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="azdaily daemon", description="Web dashboard + ALAS supervisor")
    parser.add_argument("--config", default=None,
                        help="daily.yaml (default: cwd, then the sidecar copy)")
    parser.add_argument("--port", type=int, default=None)
    parser.add_argument("--host", default=None)
    parser.add_argument("--token", default=None)
    parser.add_argument("--no-browser", action="store_true",
                        help="do not open the browser on start")
    args = parser.parse_args(argv)

    config_path = Path(args.config) if args.config else (
        Path("daily.yaml") if Path("daily.yaml").exists() else SIDE / "daily.yaml")
    try:
        cfg = load_daily_config(config_path)
    except (OSError, ValueError) as e:
        print(f"FAIL  daily config  {e}", file=sys.stderr)
        return 1

    from azdaily import VERSION  # late import avoids a cycle at module load
    daemon = Dashboard(cfg, version=VERSION)
    daemon._config_file = config_path

    host, port = cfg.daemon.listen.rsplit(":", 1)
    if args.host is not None:
        host = args.host
    if args.port is not None:
        port = args.port
    if args.token is not None:
        from dataclasses import replace
        cfg = replace(cfg, daemon=replace(cfg.daemon, token=args.token or None))
    if host not in ("127.0.0.1", "localhost", "::1") and not cfg.daemon.token:
        print("FAIL  binding a non-localhost address requires --token "
              "(or daemon.token in daily.yaml)", file=sys.stderr)
        return 1

    port_i = int(port)
    daemon = Dashboard(cfg, version=VERSION, config_file=config_path)
    server = daemon.make_server(host, port_i)
    url = f"http://{host if host != '0.0.0.0' else '127.0.0.1'}:{port_i}"
    print(f"azdaily daemon v{VERSION} — dashboard: {url}")
    print(f"  always_on={cfg.daemon.always_on} run_at={cfg.daemon.run_at} "
          f"watchdog={cfg.daemon.watchdog} config={config_path}")
    if not args.no_browser and not os.environ.get("INVOCATION_ID"):
        threading.Timer(1.0, webbrowser.open, args=(url,)).start()

    threading.Thread(target=daemon.supervisor_loop, daemon=True).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\ndaemon stopped")
    finally:
        daemon.shutdown.set()
        server.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
