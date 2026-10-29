"""Preflight checks for the Waydroid session used by the daily orchestrator.

ADB is always run through `docker exec <container> adb ...` so the host never
opens its own adb server (port 5037 would clash with ALAS).
"""
from __future__ import annotations

import re
import subprocess
from typing import Protocol

from daily_config import DeviceCfg

CMD_TIMEOUT = 15
_PHYSICAL_SIZE_RE = re.compile(r"Physical size:\s*(\d+)x(\d+)")


class Runner(Protocol):
    def run(self, args: list[str], timeout: float) -> tuple[int, str]:
        """Run a command; return (returncode, stdout and stderr combined)."""
        ...


class SubprocessRunner:
    def run(self, args: list[str], timeout: float) -> tuple[int, str]:
        try:
            proc = subprocess.run(
                args, capture_output=True, text=True, timeout=timeout
            )
        except subprocess.TimeoutExpired:
            return (124, f"timeout after {timeout}s: {' '.join(args)}")
        except OSError as e:
            return (127, f"cannot run {args[0]}: {e}")
        return (proc.returncode, (proc.stdout or "") + (proc.stderr or ""))


def check_waydroid(runner: Runner, container: str, device: DeviceCfg) -> list[str]:
    """Return a list of human-readable problems; empty list means all good."""
    problems: list[str] = []

    # 1. Waydroid session must be RUNNING. Later steps are pointless otherwise.
    rc, out = runner.run(["waydroid", "status"], CMD_TIMEOUT)
    if rc != 0 or "RUNNING" not in out:
        problems.append(
            "Waydroid session is not running (run `waydroid session start`)"
        )
        return problems

    # 2. The ALAS container must exist. Whether it is running is decided by
    #    the orchestrator (reported as already_running), so the output is
    #    not checked here.
    rc, _ = runner.run(
        ["docker", "inspect", "-f", "{{.State.Running}}", container], CMD_TIMEOUT
    )
    if rc != 0:
        problems.append(f"ALAS container '{container}' not found")

    # 3. Screen resolution must match the profile (via the container's adb).
    want_w, want_h = device.resolution
    rc, out = runner.run(
        ["docker", "exec", container, "adb", "-s", device.serial,
         "shell", "wm", "size"],
        CMD_TIMEOUT,
    )
    m = _PHYSICAL_SIZE_RE.search(out) if rc == 0 else None
    if m is None:
        problems.append(
            f"cannot read screen size from {device.serial} via container "
            f"'{container}' (expected {want_w}x{want_h})"
        )
    elif (int(m.group(1)), int(m.group(2))) != (want_w, want_h):
        problems.append(
            f"screen size is {m.group(1)}x{m.group(2)}, expected {want_w}x{want_h}"
        )

    return problems
