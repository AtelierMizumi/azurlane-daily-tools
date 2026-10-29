"""Check and apply the ALAS config values the daily orchestrator depends on.

ALAS stores its config as nested JSON; keys are dotted paths in deep_get/deep_set
(see AzurLaneAutoScript/module/config/deep.py), so ("Commission", "Scheduler", "Enable")
maps to data["Commission"]["Scheduler"]["Enable"].
"""

import json
import shutil
from pathlib import Path

REQUIRED: dict[tuple[str, ...], object] = {
    ("GameManager", "AutoRestart"): True,
    ("Commission", "Scheduler", "Enable"): True,
}

_MISSING = object()


def _get(data: dict, keys: tuple[str, ...]) -> object:
    cur: object = data
    for key in keys:
        if not isinstance(cur, dict):
            return _MISSING
        cur = cur.get(key, _MISSING)
    return cur


def _set(data: dict, keys: tuple[str, ...], value: object) -> None:
    cur = data
    for key in keys[:-1]:
        cur = cur.setdefault(key, {})
    cur[keys[-1]] = value


def check_alas_config(data: dict) -> list[str]:
    problems = []
    for keys, expected in REQUIRED.items():
        path = ".".join(keys)
        actual = _get(data, keys)
        if actual is _MISSING:
            problems.append(f"{path} is missing, expected {expected!r}")
        elif actual != expected:
            problems.append(f"{path} is {actual!r}, expected {expected!r}")
    return problems


def load_alas_config(path: Path) -> dict:
    if not path.exists():
        raise FileNotFoundError(f"Không tìm thấy {path}: mở ALAS GUI một lần để tạo config.")
    return json.loads(path.read_text(encoding="utf-8"))


def apply_alas_config(path: Path) -> Path:
    data = load_alas_config(path)
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    for keys, value in REQUIRED.items():
        _set(data, keys, value)
    path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    return backup
