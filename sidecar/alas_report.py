"""Read-only report of what ALAS did per scheduled task, from its log file.

Usage: uv run python alas_report.py --log PATH
Never writes any file.
"""
import argparse
import re
import sys
from datetime import datetime, timedelta
from pathlib import Path

TS_RE = re.compile(r"(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)(?:\.(\d{1,6}))?")
START_RE = re.compile(r"Scheduler: Start task `(\w+)`")
END_RE = re.compile(r"Scheduler: End task `(\w+)`")
# The datetime may wrap onto the next line, so this runs on the whole text.
NEXT_RE = re.compile(
    r"Save config \S+, (\w+)\.Scheduler\.NextRun=datetime\.datetime\("
    r"\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+),\s*(\d+)\s*\)"
)
RESTARTING_RE = re.compile(r"Restarting (\S+) to fix it")
ABORT_RE = re.compile(r"Aborted \(core dumped\)")
FMT = "%Y-%m-%d %H:%M:%S"


def _ts(line):
    m = TS_RE.search(line)
    if not m:
        return None
    return datetime.strptime(m.group(1) + "." + (m.group(2) or "0").ljust(6, "0"),
                             FMT + ".%f")


def _fmt(dt):
    return dt.strftime(FMT) if dt else None


def build_report(text):
    tasks, ends, last_ts = [], {}, None
    counts = {"restarts": 0, "game_not_running": 0, "errors": 0, "aborts": 0}
    current = None
    for line in text.splitlines():
        ts = _ts(line)
        if ts:
            last_ts = ts
        if START_RE.search(line):
            current = {"name": START_RE.search(line).group(1), "_start": ts, "_end": None,
                       "commission_scan": False, "status": "incomplete"}
            tasks.append(current)
        elif END_RE.search(line):
            name = END_RE.search(line).group(1)
            task = next((t for t in reversed(tasks) if t["name"] == name and t["_end"] is None
                         and t["status"] == "incomplete"), None)
            if task:
                task["_end"], task["status"] = ts, "done"
            if ts:
                ends[name] = max(ts, ends.get(name, ts))
            if task is current:
                current = None
        elif "Commission detect" in line and current and current["name"] == "Commission":
            current["commission_scan"] = True
        if RESTARTING_RE.search(line):
            counts["restarts"] += 1
        if "Game not running" in line:
            counts["game_not_running"] += 1
        if "| ERROR |" in line:
            counts["errors"] += 1
        if ABORT_RE.search(line):
            counts["aborts"] += 1

    next_runs = {}
    for m in NEXT_RE.finditer(text):
        try:
            next_runs[m.group(1)] = datetime(*(int(g) for g in m.groups()[1:]))
        except ValueError:
            continue

    out_tasks = []
    for t in tasks:
        is_commission = t["name"] == "Commission"
        status = t["status"]
        if is_commission and status == "done" and not t["commission_scan"]:
            status = "no_commission"
        start, end = t["_start"], t["_end"]
        out_tasks.append({
            "name": t["name"], "started": _fmt(start), "ended": _fmt(end),
            "duration_s": (end - start).total_seconds() if start and end else None,
            "commission_scan": t["commission_scan"] if is_commission else None,
            "status": status,
        })

    anomalies = []
    for i, t in enumerate(out_tasks):
        if t["status"] == "no_commission":
            anomalies.append(f"{t['name']} ended at {t['ended']} without a commission scan")
        elif t["status"] == "incomplete" and i < len(out_tasks) - 1:
            anomalies.append(f"{t['name']} started at {t['started']} but is incomplete "
                             f"(no End before the next task)")
    for name, nxt in next_runs.items():
        if name in ends and nxt < ends[name]:
            anomalies.append(f"{name} NextRun {_fmt(nxt)} is earlier than its last End "
                             f"{_fmt(ends[name])} (not updated)")
    restart = next_runs.get("Restart")
    if restart and last_ts and restart - last_ts > timedelta(hours=24):
        anomalies.append(f"Restart scheduled far ahead; check server_update delay "
                         f"(NextRun {_fmt(restart)}, last log {_fmt(last_ts)})")
    if counts["restarts"] + counts["aborts"] > 0:
        anomalies.append(f"{counts['restarts']} restart(s) and {counts['aborts']} abort(s) "
                         f"in this log")

    return {"tasks": out_tasks, "next_runs": {k: _fmt(v) for k, v in next_runs.items()},
            **counts, "anomalies": anomalies}


def format_report(report):
    lines = [f"{'task':<12} {'status':<14} {'started':<19} {'ended':<19} {'dur_s':>7}  next_run"]
    for t in report["tasks"]:
        dur = "-" if t["duration_s"] is None else f"{t['duration_s']:.1f}"
        lines.append(f"{t['name']:<12} {t['status']:<14} {t['started'] or '-':<19} "
                     f"{t['ended'] or '-':<19} {dur:>7}  {report['next_runs'].get(t['name'], '-')}")
    lines += [f"ANOMALY: {a}" for a in report["anomalies"]]
    lines.append(f"restarts={report['restarts']} game_not_running={report['game_not_running']} "
                 f"errors={report['errors']} aborts={report['aborts']}")
    return "\n".join(lines)


def main(argv=None):
    ap = argparse.ArgumentParser(description="Read-only ALAS schedule report")
    ap.add_argument("--log", required=True, help="path to an ALAS log file")
    args = ap.parse_args(argv)
    path = Path(args.log)
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as e:
        print(f"error: cannot read {path}: {e}", file=sys.stderr)
        return 1
    print(format_report(build_report(text)))
    return 0


if __name__ == "__main__":
    sys.exit(main())
