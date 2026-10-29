"""Loader for the daily orchestrator config (daily.yaml)."""
from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import yaml

SUPPORTED_PROFILE = "waydroid"
DEFAULT_RUN_MINUTES = 60
_RESOLUTION_RE = re.compile(r"^(\d+)x(\d+)$")


@dataclass(frozen=True)
class DeviceCfg:
    profile: str
    serial: str
    resolution: tuple[int, int]


@dataclass(frozen=True)
class DailyConfig:
    device: DeviceCfg
    alas_container: str
    alas_webui_url: str
    alas_repo_dir: Path
    run_minutes: int
    ntfy_url: str | None


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

    device = DeviceCfg(
        profile=profile,
        serial=str(_require(data, "device.serial")),
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
    )
