"""Check and apply the ALAS config values the daily orchestrator depends on.

ALAS stores its config as nested JSON; keys are dotted paths in deep_get/deep_set
(see AzurLaneAutoScript/module/config/deep.py), so ("Commission", "Scheduler", "Enable")
maps to data["Commission"]["Scheduler"]["Enable"].
"""

import json
import re
import shutil
from datetime import datetime
from pathlib import Path

# ALAS schedules per task: get_next_task() iterates the top-level entries of
# alas.json and each Function() reads "Scheduler.Enable" INSIDE that task
# (module/config/config.py), so the key the Commission task needs is
# Commission.Scheduler.Enable. There is no global top-level Scheduler key.
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


def check_alas_config(data: dict, deploy_path: Path | None = None) -> list[str]:
    """Problems with alas.json and, when deploy_path is given, with deploy.yaml's Run."""
    problems = []
    for keys, expected in REQUIRED.items():
        path = ".".join(keys)
        actual = _get(data, keys)
        if actual is _MISSING:
            problems.append(f"{path} is missing, expected {expected!r}")
        elif actual != expected:
            problems.append(f"{path} is {actual!r}, expected {expected!r}")
    if deploy_path is not None:
        problems.extend(check_deploy_run(deploy_path))
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


# --- deploy.yaml `Run` (Ruling 32) -------------------------------------------
# The container runs `python gui.py`; app.py only auto-starts instances when
# deploy.yaml has Run set. ALAS reads deploy.yaml with a line-based reader
# (deploy/utils.py poor_yaml_read) and later splits the string on commas, so
# the value is written in the bracketed list form the template comment shows.

INSTANCE = "alas"  # alas.json is the instance named "alas"
RUN_LINE = f'Run: ["{INSTANCE}"]'
_RUN_RE = re.compile(r"^([ \t]*)Run:([^\r\n]*)", re.MULTILINE)


def _run_instances(value: str) -> list[str]:
    """Mirror of module/webui/app.py: split on commas, strip brackets and quotes."""
    value = value.strip("\n\r\t' ")
    if not value or value.lower() == "null":
        return []
    return [part.strip(" ['\"]") for part in value.split(",") if len(part)]


def check_deploy_run(path: Path) -> list[str]:
    text = Path(path).read_text(encoding="utf-8")  # OSError if missing/unreadable
    m = _RUN_RE.search(text)
    if m is None:
        return [f"deploy.yaml has no Run key, expected {RUN_LINE}"]
    if INSTANCE not in _run_instances(m.group(2)):
        return [f"deploy.yaml Run is {m.group(2).strip() or 'empty'}, expected {RUN_LINE}"]
    return []


def apply_deploy_run(path: Path) -> Path:
    """Set Run to ["alas"] with a targeted line replacement; return the backup path.

    The file is edited as text (not re-dumped as YAML) so comments and layout stay.
    """
    path = Path(path)
    raw = path.read_bytes()
    text = raw.decode("utf-8")
    backup = path.with_name(path.name + ".bak")
    shutil.copy2(path, backup)
    m = _RUN_RE.search(text)
    if m is not None:
        text = text[:m.start()] + m.group(1) + RUN_LINE + text[m.end():]
    else:
        newline = "\r\n" if "\r\n" in text else "\n"
        if text and not text.endswith("\n"):
            text += newline
        text += RUN_LINE + newline
    path.write_bytes(text.encode("utf-8"))
    return backup


# --- NextRun (Ruling 33) -----------------------------------------------------

def commission_next_run_warning(data: dict, now: datetime | None = None) -> str | None:
    """A warning when Commission.Scheduler.NextRun is in the future, else None.

    ALAS will not start the task before NextRun, so a run can time out waiting.
    """
    value = _get(data, ("Commission", "Scheduler", "NextRun"))
    if not isinstance(value, str):
        return None
    try:
        next_run = datetime.fromisoformat(value)
        now = now or datetime.now()
        if next_run <= now:
            return None
    except (ValueError, TypeError):
        return None
    return (f"WARNING: NextRun {value} is in the future; ALAS will not start "
            f"Commission before then")
