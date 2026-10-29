"""Loader for the daily orchestrator config (daily.yaml)."""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_PROFILE = "waydroid"
DEFAULT_RUN_MINUTES = 60
DEFAULT_LISTEN = "127.0.0.1:22268"
_RESOLUTION_RE = re.compile(r"^(\d+)x(\d+)$")
_RUN_AT_RE = re.compile(r"^([01]?\d|2[0-3]):([0-5]\d)$")


@dataclass(frozen=True)
class DeviceCfg:
    profile: str
    serial: str
    resolution: tuple[int, int]


@dataclass(frozen=True)
class DaemonCfg:
    """daemon/dashboard section; every field is optional in daily.yaml."""
    listen: str = DEFAULT_LISTEN
    token: str | None = None
    always_on: bool = True
    run_at: str | None = None
    watchdog: bool = True
    watchdog_interval: int = 60


def _parse_listen(raw: Any) -> str:
    """Accept "host:port", ":port" (localhost implied) or a bare port number."""
    text = str(raw).strip()
    if text.isdigit():
        host, port = "127.0.0.1", text
    elif ":" in text:
        host, port = text.rsplit(":", 1)
        host = host or "127.0.0.1"
    else:
        raise ValueError(f"daily config: daemon.listen '{text}' must look like host:port")
    if not port.isdigit() or not 1 <= int(port) <= 65535:
        raise ValueError(f"daily config: daemon.listen '{text}' has an invalid port")
    return f"{host}:{port}"


def _daemon_cfg(raw: Any) -> DaemonCfg:
    if raw is None:
        return DaemonCfg()
    if not isinstance(raw, dict):
        raise ValueError("daily config: daemon section must be a mapping")
    listen = _parse_listen(raw["listen"]) if raw.get("listen") is not None else DaemonCfg.listen

    run_at = raw.get("run_at")
    if run_at is not None:
        run_at = str(run_at).strip()
        if _RUN_AT_RE.match(run_at) is None:
            raise ValueError(f"daily config: daemon.run_at '{run_at}' must look like HH:MM")

    watchdog_interval = raw.get("watchdog_interval")
    if watchdog_interval is not None:
        watchdog_interval = int(watchdog_interval)
        if watchdog_interval < 10:
            raise ValueError("daily config: daemon.watchdog_interval must be >= 10 seconds")

    token = raw.get("token")
    token = str(token) if token not in (None, "") else None

    return DaemonCfg(
        listen=listen,
        token=token,
        always_on=bool(raw.get("always_on", True)),
        run_at=run_at,
        watchdog=bool(raw.get("watchdog", True)),
        watchdog_interval=watchdog_interval or DaemonCfg.watchdog_interval,
    )


@dataclass(frozen=True)
class DailyConfig:
    device: DeviceCfg
    alas_container: str
    alas_webui_url: str
    alas_repo_dir: Path
    run_minutes: int
    ntfy_url: str | None
    daemon: DaemonCfg = field(default_factory=DaemonCfg)


def _require(d: dict, dotted: str) -> Any:
    """Return the value at dotted key path (e.g. "alas.container_name").

    Raises ValueError naming the full path when any segment is missing.
    A key present with a null value is returned as None (it is not missing).
    """
    node: Any = d
    for part in dotted.split("."):
        if not isinstance(node, dict) or part not in node:
            raise ValueError(f"daily config: missing required key '{dotted}'")
        node = node[part]
    return node


def load_daily_config(path: Path) -> DailyConfig:
    path = Path(path)
    data = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    if not isinstance(data, dict):
        raise ValueError("daily config: top level must be a mapping")

    profile = _require(data, "device.profile")
    if profile != SUPPORTED_PROFILE:
        raise ValueError(
            f"daily config: device.profile '{profile}' is not supported yet "
            f"(only '{SUPPORTED_PROFILE}')"
        )

    raw_resolution = str(_require(data, "device.resolution"))
    match = _RESOLUTION_RE.match(raw_resolution)
    if match is None:
        raise ValueError(
            f"daily config: device.resolution '{raw_resolution}' must look like WxH, e.g. 1280x720"
        )
    resolution = (int(match.group(1)), int(match.group(2)))

    raw_serial = _require(data, "device.serial")
    serial = "" if raw_serial is None else str(raw_serial).strip()
    if not serial:
        raise ValueError("daily config: 'device.serial' must not be empty (e.g. 192.0.2.10:5555)")

    device = DeviceCfg(
        profile=profile,
        serial=serial,
        resolution=resolution,
    )

    repo_dir = Path(str(_require(data, "alas.repo_dir")))
    if not repo_dir.is_absolute():
        repo_dir = (path.parent / repo_dir).resolve()

    run_minutes = DEFAULT_RUN_MINUTES
    timeouts = data.get("timeouts")
    if isinstance(timeouts, dict) and timeouts.get("run_minutes") is not None:
        run_minutes = int(timeouts["run_minutes"])

    return DailyConfig(
        device=device,
        alas_container=str(_require(data, "alas.container_name")),
        alas_webui_url=str(_require(data, "alas.webui_url")),
        alas_repo_dir=repo_dir,
        run_minutes=run_minutes,
        ntfy_url=_require(data, "notify.ntfy_url"),
        daemon=_daemon_cfg(data.get("daemon")),
    )
