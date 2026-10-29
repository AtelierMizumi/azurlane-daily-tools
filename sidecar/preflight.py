"""Preflight checks for the Waydroid session used by the daily orchestrator.

ADB is always run through `docker exec <container> adb ...` so the host never
opens its own adb server (port 5037 would clash with ALAS).

Two phases (Ruling 31): `check_waydroid` runs BEFORE `docker start` and never
uses `docker exec` (it would fail on a stopped container); `check_resolution`
runs AFTER `docker start`. Every problem names the rc or output and a remedy
(Ruling 38).
"""
from __future__ import annotations

import re
import socket
import subprocess
from typing import Protocol

from daily_config import DeviceCfg

CMD_TIMEOUT = 15
HOST_ADB_PORT = 5037
_PHYSICAL_SIZE_RE = re.compile(r"Physical size:\s*(\d+)x(\d+)")
_SESSION_RE = re.compile(r"^Session:\s*(\S+)", re.MULTILINE)
_CONTAINER_RE = re.compile(r"^Container:\s*(\S+)", re.MULTILINE)
_ADB_CONNECT_FAIL_RE = re.compile(r"\b(failed|cannot|unable|error)\b", re.IGNORECASE)


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


def _real_adb_port_open(host: str = "127.0.0.1", port: int = HOST_ADB_PORT,
                        timeout: float = 1.0) -> bool:
    """True if something accepts TCP connections on host:port.

    A plain socket connect: running the adb binary would itself start a server.
    """
    try:
        with socket.create_connection((host, port), timeout=timeout):
            return True
    except OSError:
        return False


# Looked up at call time so tests can replace it.
_adb_port_open = _real_adb_port_open


def _failure(cmd: str, rc: int, out: str) -> str:
    """Describe a command that did not succeed; 124 and 127 are named as such."""
    out = out.strip()
    if rc == 124:
        kind = "timed out"
    elif rc == 127:
        kind = "cannot run (is it installed and on PATH?)"
    else:
        kind = "failed"
    return f"`{cmd}` {kind} (rc={rc}: {out})"


def check_waydroid(runner: Runner, container: str, device: DeviceCfg) -> list[str]:
    """Checks that must hold BEFORE `docker start`; empty list means all good.

    Never calls `docker exec`: the container is normally stopped here.
    """
    problems: list[str] = []

    # 1. Waydroid session must be RUNNING. Later steps are pointless otherwise.
    rc, out = runner.run(["waydroid", "status"], CMD_TIMEOUT)
    if rc != 0:
        remedy = ("install waydroid" if rc == 127 else "run `waydroid session start`")
        problems.append(f"{_failure('waydroid status', rc, out)}; {remedy}")
        return problems
    session = _SESSION_RE.search(out)
    if session is None or session.group(1) != "RUNNING":
        seen = f"Session: {session.group(1)}" if session else f"no Session line in: {out.strip()}"
        problems.append(
            f"Waydroid session is not running; run `waydroid session start` ({seen})"
        )
        return problems

    # 2. A FROZEN container cannot serve adb; launching the game thaws it.
    container_state = _CONTAINER_RE.search(out)
    if container_state is not None and container_state.group(1) == "FROZEN":
        problems.append(
            "Waydroid container is FROZEN; "
            "run `waydroid app launch com.YoStarEN.AzurLane` (Container: FROZEN)"
        )

    # 3. The ALAS container must exist. Whether it is running is decided by
    #    the orchestrator (reported as already_running), so the output is
    #    not checked here.
    rc, out = runner.run(
        ["docker", "inspect", "--type", "container", "-f", "{{.State.Running}}", container],
        CMD_TIMEOUT,
    )
    if rc != 0:
        if rc not in (124, 127) and "no such" in out.lower():
            problems.append(
                f"ALAS container '{container}' not found (rc={rc}: {out.strip()}); "
                f"create it (`docker compose up --no-start`) or fix alas.container_name in daily.yaml"
            )
        else:
            remedy = "install docker" if rc == 127 else "run `docker ps` to check the Docker daemon"
            problems.append(f"{_failure('docker inspect', rc, out)}; {remedy}")

    # 4. The host must not run its own adb server (it would fight ALAS's).
    if _adb_port_open("127.0.0.1", HOST_ADB_PORT, 1.0):
        problems.append(
            f"host adb server is running on :{HOST_ADB_PORT}; run `adb kill-server`"
        )

    return problems


def check_resolution(runner: Runner, container: str, device: DeviceCfg) -> list[str]:
    """Checks that need the container RUNNING (call after `docker start`).

    Connects the container's adb to the device, then compares the screen size
    with the profile.
    """
    serial = device.serial
    want_w, want_h = device.resolution

    connect = f"docker exec {container} adb connect {serial}"
    rc, out = runner.run(["docker", "exec", container, "adb", "connect", serial], CMD_TIMEOUT)
    if rc != 0 or _ADB_CONNECT_FAIL_RE.search(out):
        detail = _failure(connect, rc, out) if rc != 0 else f"`{connect}` failed (rc={rc}: {out.strip()})"
        return [f"{detail}; check that the Waydroid session is up and device.serial "
                f"({serial}) in daily.yaml is correct"]

    wm = f"docker exec {container} adb -s {serial} shell wm size"
    rc, out = runner.run(
        ["docker", "exec", container, "adb", "-s", serial, "shell", "wm", "size"],
        CMD_TIMEOUT,
    )
    if rc != 0:
        return [f"{_failure(wm, rc, out)}; check that the container is running and "
                f"adb is connected to {serial}"]
    m = _PHYSICAL_SIZE_RE.search(out)
    if m is None:
        return [f"cannot read screen size from `{wm}` (rc={rc}: {out.strip()}; "
                f"expected {want_w}x{want_h}); check that Waydroid is fully booted"]
    if (int(m.group(1)), int(m.group(2))) != (want_w, want_h):
        return [f"screen size is {m.group(1)}x{m.group(2)}, expected {want_w}x{want_h}; "
                f"run `waydroid prop set persist.waydroid.width {want_w}` and "
                f"`waydroid prop set persist.waydroid.height {want_h}`, then restart the session"]
    return []
