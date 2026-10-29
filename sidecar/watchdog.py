#!/usr/bin/env python3
"""ALAS watchdog sidecar.

Watches the ALAS Docker container, its WebUI and its log files; detects
stalls on the Waydroid screen; reports anomalies via ntfy. Read-only with
respect to ALAS: it never clicks or edits game state.

Usage:
    uv run watchdog.py [-c watchdog.yaml]

Checks performed every `watch.interval_seconds`:
  1. Container running?          (docker inspect)
  2. WebUI reachable?            (HTTP GET)
  3. ALAS logs fresh?            (newest log/*.txt mtime)
  4. Screen stalled?             (two adb screencaps compared via container adb)
"""

from __future__ import annotations

import argparse
import datetime as dt
import io
import logging
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import yaml
from PIL import Image, ImageChops

log = logging.getLogger("watchdog")

DEFAULT_CONFIG = Path(__file__).with_name("watchdog.yaml")


class Config:
    def __init__(self, raw: dict, base_dir: Path):
        alas = raw["alas"]
        watch = raw["watch"]
        notify = raw.get("notify", {})
        self.container = alas["container_name"]
        self.webui = alas["webui_url"]
        self.repo_dir = (base_dir / alas["repo_dir"]).resolve()
        self.interval = int(watch["interval_seconds"])
        self.log_stale = int(watch["log_stale_seconds"])
        self.stall_seconds = int(watch["stall_seconds"])
        self.ntfy_url = notify.get("ntfy_url")
        self.fail_threshold = int(notify.get("consecutive_failures", 2))


def run(cmd: list[str], timeout: int = 20) -> subprocess.CompletedProcess:
    """Bytes mode: screencap output is binary PNG, never decode it blindly."""
    return subprocess.run(cmd, capture_output=True, timeout=timeout)


def container_running(container: str) -> bool:
    try:
        p = run(["docker", "inspect", "-f", "{{.State.Running}}", container], timeout=10)
        return p.returncode == 0 and p.stdout.decode().strip() == "true"
    except (subprocess.TimeoutExpired, OSError) as e:
        log.error("docker inspect failed: %s", e)
        return False


def webui_alive(url: str) -> bool:
    try:
        with urllib.request.urlopen(url, timeout=10) as r:
            return r.status == 200
    except OSError:
        return False


def newest_log_age(repo_dir: Path) -> float | None:
    """Age in seconds of the most recently modified file under log/."""
    log_dir = repo_dir / "log"
    if not log_dir.is_dir():
        return None
    newest = max(
        (f.stat().st_mtime for f in log_dir.rglob("*.txt") if f.is_file()),
        default=None,
    )
    if newest is None:
        return None
    return time.time() - newest


class ScreenChecker:
    """Stall detection: capture the game screen through the container's adb
    and compare consecutive frames."""

    def __init__(self, container: str, serial: str):
        self.container = container
        self.serial = serial
        self.last_frame: Image.Image | None = None
        self.last_change = time.time()

    def _screencap(self) -> Image.Image | None:
        cmd = ["docker", "exec", self.container,
               "adb", "-s", self.serial, "exec-out", "screencap", "-p"]
        try:
            p = run(cmd, timeout=30)
        except subprocess.TimeoutExpired:
            return None
        if p.returncode != 0 or not p.stdout:
            return None
        try:
            return Image.open(io.BytesIO(p.stdout)).convert("L")
        except Exception:
            return None

    def stalled(self) -> bool:
        frame = self._screencap()
        if frame is None:
            # Capture failure is reported by container/webui checks; skip here.
            return False
        if self.last_frame is not None:
            diff = ImageChops.difference(frame, self.last_frame)
            changed = diff.getbbox() is not None
            if changed:
                self.last_change = time.time()
        self.last_frame = frame
        return (time.time() - self.last_change) > self.stall_seconds


class Notifier:
    def __init__(self, url: str | None, threshold: int):
        self.url = url
        self.threshold = threshold
        self._fail_counts: dict[str, int] = {}

    def record_failure(self, kind: str, detail: str) -> None:
        n = self._fail_counts.get(kind, 0) + 1
        self._fail_counts[kind] = n
        log.warning("%s (fail #%d): %s", kind, n, detail)
        if n == self.threshold:
            self.push(kind, detail)

    def record_recovery(self, kind: str) -> None:
        if self._fail_counts.pop(kind, None):
            log.info("%s recovered", kind)
            self.push(f"{kind}-recovered", "checks pass again")

    def push(self, title: str, body: str) -> None:
        if not self.url:
            return
        try:
            req = urllib.request.Request(
                self.url, data=body.encode(), method="POST",
                headers={"Title": f"ALAS watchdog: {title}", "Tags": "warning"},
            )
            urllib.request.urlopen(req, timeout=10)
        except OSError as e:
            log.error("ntfy push failed: %s", e)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("-c", "--config", default=str(DEFAULT_CONFIG))
    args = parser.parse_args()
    cfg_path = Path(args.config).resolve()
    cfg = Config(yaml.safe_load(cfg_path.read_text()), cfg_path.parent)

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
        handlers=[
            logging.StreamHandler(),
            logging.FileHandler(Path(__file__).with_name("watchdog.log")),
        ],
    )
    log.info("watchdog started, repo=%s webui=%s", cfg.repo_dir, cfg.webui)

    # Serial is the Waydroid adb endpoint; screencaps run through the
    # container's own adb so the host never starts an adb server.
    screen = ScreenChecker(cfg.container, "192.168.240.112:5555")
    notifier = Notifier(cfg.ntfy_url, cfg.fail_threshold)

    while True:
        if not container_running(cfg.container):
            screen.last_frame = None
            screen.last_change = time.time()
            notifier.record_failure("container-down", "docker container not running")
        else:
            notifier.record_recovery("container-down")

        if not webui_alive(cfg.webui):
            notifier.record_failure("webui-down", f"no HTTP 200 from {cfg.webui}")
        else:
            notifier.record_recovery("webui-down")

        age = newest_log_age(cfg.repo_dir)
        if age is not None and age > cfg.log_stale:
            notifier.record_failure(
                "log-stale", f"newest ALAS log is {int(age)}s old (>{cfg.log_stale}s)")
        else:
            notifier.record_recovery("log-stale")

        if screen.stalled():
            notifier.record_failure(
                "screen-stall", f"game screen unchanged for >{cfg.stall_seconds}s")
        else:
            notifier.record_recovery("screen-stall")

        time.sleep(cfg.interval)


if __name__ == "__main__":
    sys.exit(main())
