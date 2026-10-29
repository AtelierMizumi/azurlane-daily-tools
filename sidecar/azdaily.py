#!/usr/bin/env python3
"""azdaily — unified entry point for azurlane-daily-tools.

One command for everything the sidecar can do, so users do not have to
remember which script implements what:

    ./azdaily init      create daily.yaml (and optionally watchdog.yaml) from the shipped examples
    ./azdaily check     preflight + ALAS config check, no container start
    ./azdaily run       full daily run, per-stage PASS/FAIL report (flags: --apply --config)
    ./azdaily apply     full run that also writes the ALAS config (container must be stopped)
    ./azdaily report    read-only ALAS schedule report (newest log by default)
    ./azdaily bot       commission claim bot (commission_bot.py passthrough)
    ./azdaily watchdog  ALAS watchdog sidecar (watchdog.py passthrough)
    ./azdaily test      full offline test suite (unit tests + state machine + bot selftest)

The scripts are designed to run from their checkout (assets and example
configs resolve relative to this file), so the repo root ships an `azdaily`
shell wrapper: `./azdaily <command>`. Calling this module directly
(`uv run python azdaily.py <command>`) is equivalent.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
from pathlib import Path

SIDE = Path(__file__).resolve().parent
VERSION = "0.2.0"

# Same module list as the documented test command (HANDOVER section 3).
TEST_MODULES = ["test_daily_config", "test_watchdog_serial", "test_alas_config",
                "test_log_parser", "test_preflight", "test_azurlane_daily",
                "test_alas_report", "test_azdaily"]

INIT_NEXT_STEPS = """\
Bước tiếp theo:
  1. Sửa {daily}: device.serial (xem `waydroid status`), alas.repo_dir.
  2. Lần đầu hoặc khi cần ghi config ALAS: ./azdaily apply
     (container phải đang dừng; nhớ chown alas.json — docs/HUONG-DAN.md mục 6).
  3. ./azdaily check rồi ./azdaily run
"""


def _config_path(rest: list[str]) -> Path:
    """The daily.yaml the orchestrator/report would read for these flags.

    Mirrors azurlane_daily's --config default, but falls back to the copy
    next to this file so ./azdaily works from any directory.
    """
    it = iter(rest)
    for arg in it:
        if arg == "--config":
            return Path(next(it))
        if arg.startswith("--config="):
            return Path(arg.split("=", 1)[1])
    cwd = Path("daily.yaml")
    if cwd.exists():
        return cwd
    return SIDE / "daily.yaml"


def cmd_check(rest: list[str]) -> int:
    from azurlane_daily import main
    return main(["--check", *rest])


def cmd_run(rest: list[str]) -> int:
    from azurlane_daily import main
    return main(list(rest))


def cmd_apply(rest: list[str]) -> int:
    from azurlane_daily import main
    return main(["--e2e", "--apply", *rest])


def cmd_report(rest: list[str]) -> int:
    if not any(a == "--log" or a.startswith("--log=") for a in rest):
        from daily_config import load_daily_config
        from log_parser import newest_log_file
        try:
            cfg = load_daily_config(_config_path(rest))
        except (OSError, ValueError) as e:
            print(f"error: cannot read the daily config to locate ALAS logs: {e}",
                  file=sys.stderr)
            return 1
        log_dir = Path(cfg.alas_repo_dir) / "log"
        newest = newest_log_file(log_dir)
        if newest is None:
            print(f"error: no log/*.txt found under {log_dir} "
                  f"(pass --log PATH to point at one)", file=sys.stderr)
            return 1
        print(f"# newest ALAS log: {newest}")
        rest = [*rest, "--log", str(newest)]
    from alas_report import main
    return main(rest)


def _subcommand_process(script: str, rest: list[str]) -> int:
    # commission_bot.main() and watchdog.main() read sys.argv directly, so
    # dispatching in-process would leak the azdaily flags into them.
    return subprocess.run([sys.executable, str(SIDE / script), *rest]).returncode


def cmd_bot(rest: list[str]) -> int:
    return _subcommand_process("commission_bot.py", rest)


def cmd_watchdog(rest: list[str]) -> int:
    return _subcommand_process("watchdog.py", rest)


def cmd_test(rest: list[str]) -> int:
    steps: list[tuple[str, list[str]]] = [
        ("unit tests", [sys.executable, "-m", "unittest", *TEST_MODULES]),
        ("state machine", [sys.executable, str(SIDE / "test_state_machine.py")]),
        ("bot selftest", [sys.executable, str(SIDE / "commission_bot.py"), "--selftest"]),
    ]
    failed = []
    for label, cmd in steps:
        print(f"=== azdaily test: {label} ===")
        if subprocess.run(cmd, cwd=SIDE).returncode != 0:
            failed.append(label)
    if failed:
        print(f"azdaily test FAILED: {', '.join(failed)}", file=sys.stderr)
        return 1
    print("azdaily test: all green")
    return 0


def _copy_example(side: Path, src: str, dst: str) -> str:
    dst_path = side / dst
    if dst_path.exists():
        return f"kept existing {dst} (remove it first to re-init)"
    src_path = side / src
    if not src_path.exists():
        return f"error: {src} is missing from the checkout, cannot create {dst}"
    dst_path.write_text(src_path.read_text(encoding="utf-8"), encoding="utf-8")
    return f"created {dst} from {src}"


def cmd_init(rest: list[str], side: Path = SIDE) -> int:
    watchdog = "--watchdog" in rest
    daily = _copy_example(side, "daily.example.yaml", "daily.yaml")
    print(daily)
    if watchdog:
        print(_copy_example(side, "watchdog.example.yaml", "watchdog.yaml"))
    if not daily.startswith("created"):
        return 1  # refused (already exists) or the example is missing
    print(INIT_NEXT_STEPS.format(daily=side / "daily.yaml"))
    return 0


COMMANDS = {
    "init": ("create daily.yaml from the shipped example (flags: --watchdog)", cmd_init),
    "check": ("preflight + config check, does not start the container", cmd_check),
    "run": ("full daily run with the per-stage report", cmd_run),
    "apply": ("full run that also writes the ALAS config (container stopped)", cmd_apply),
    "report": ("ALAS schedule report, newest log by default (flags: --log)", cmd_report),
    "bot": ("commission claim bot (commission_bot.py flags)", cmd_bot),
    "watchdog": ("ALAS watchdog sidecar (watchdog.py flags)", cmd_watchdog),
    "test": ("offline test suite: unit tests + state machine + bot selftest", cmd_test),
}


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="azdaily", description="azurlane-daily-tools unified entry point")
    parser.add_argument("--version", action="version", version=f"azdaily {VERSION}")
    sub = parser.add_subparsers(dest="command", required=True, metavar="command")
    for name, (help_text, _fn) in COMMANDS.items():
        sp = sub.add_parser(name, help=help_text)
        sp.add_argument("rest", nargs=argparse.REMAINDER, help=argparse.SUPPRESS)
    args = parser.parse_args(argv)
    if str(SIDE) not in sys.path:
        sys.path.insert(0, str(SIDE))
    return COMMANDS[args.command][1](args.rest)


if __name__ == "__main__":
    sys.exit(main())
