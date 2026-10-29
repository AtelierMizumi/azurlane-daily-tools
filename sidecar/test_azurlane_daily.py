import json, tempfile, unittest
from pathlib import Path
from daily_config import DailyConfig, DeviceCfg
from azurlane_daily import run_daily
from test_preflight import FakeRunner, OK

DONE = "Scheduler: End task `Commission`"


def make_cfg():
    return DailyConfig(
        device=DeviceCfg("waydroid", "192.168.240.112:5555", (1280, 720)),
        alas_container="alas", alas_webui_url="http://x",
        alas_repo_dir=Path("."), run_minutes=1, ntfy_url=None)

GOOD_ALAS = {"GameManager": {"AutoRestart": True}, "Commission": {"Scheduler": {"Enable": True}}}
BAD_ALAS = {"GameManager": {"AutoRestart": False}, "Commission": {"Scheduler": {"Enable": True}}}

# Container stopped before start (check_waydroid only checks inspect's exit code).
STOPPED = {**OK, "inspect": (0, "false"), "docker start": (0, "alas"), "docker stop": (0, "alas")}


class Clock:
    def __init__(self): self.t = 0.0
    def __call__(self): return self.t
    def advance(self, dt): self.t += dt


def alas_json(d, data):
    p = Path(d) / "alas.json"
    p.write_text(json.dumps(data))
    return p


class AppendOnFirstSleep:
    """sleep callback: advances the clock; on its first call appends lines to a log file."""
    def __init__(self, clock, log_file, lines):
        self.clock, self.log_file, self.lines, self.done = clock, log_file, lines, False

    def __call__(self, s):
        if not self.done:
            self.done = True
            with open(self.log_file, "a", encoding="utf-8") as f:
                f.write("".join(line + "\n" for line in self.lines))
        self.clock.advance(s)


class Orchestrator(unittest.TestCase):
    def test_success_path_stops_container(self):
        d = tempfile.mkdtemp()
        runner = FakeRunner(STOPPED)
        clock = Clock()
        log_dir = Path(d)
        # lines are written DURING the run (a new log file appears after start)
        sleep = AppendOnFirstSleep(clock, log_dir / "run.txt",
                                   ["App start", "Login success", "Commission detect", DONE])
        out = run_daily(make_cfg(), runner, lambda u: 200, clock, sleep,
                        alas_json(d, GOOD_ALAS), log_dir)
        self.assertEqual(out["result"], "success")
        self.assertTrue(any(c[:2] == ["docker", "stop"] for c in runner.calls))

    def test_timeout_still_stops_container(self):
        d = tempfile.mkdtemp()
        runner = FakeRunner(STOPPED)
        clock = Clock()
        out = run_daily(make_cfg(), runner, lambda u: 200, clock, lambda s: clock.advance(s),
                        alas_json(d, GOOD_ALAS), Path(d))  # log rỗng -> timeout
        self.assertEqual(out["result"], "timeout")
        self.assertTrue(any(c[:2] == ["docker", "stop"] for c in runner.calls))

    def test_already_running_not_stopped(self):
        d = tempfile.mkdtemp()
        runner = FakeRunner({**OK, "inspect": (0, "true")})  # đang chạy sẵn
        out = run_daily(make_cfg(), runner, lambda u: 200, Clock(), lambda s: None,
                        alas_json(d, GOOD_ALAS), Path(d))
        self.assertEqual(out["result"], "already_running")
        self.assertFalse(any(c[:2] == ["docker", "stop"] for c in runner.calls))
        self.assertFalse(any(c[:2] == ["docker", "start"] for c in runner.calls))

    def test_missing_alas_config_reports_not_crash(self):
        out = run_daily(make_cfg(), FakeRunner({**OK, "inspect": (0, "false")}), lambda u: 200,
                        Clock(), lambda s: None, Path(tempfile.mkdtemp()) / "alas.json", Path("."))
        self.assertEqual(out["result"], "config_failed")

    def test_preexisting_done_line_ignored(self):
        d = tempfile.mkdtemp()
        log_dir = Path(d)
        (log_dir / "run.txt").write_text(f"App start\nLogin success\nCommission detect\n{DONE}\n")
        runner = FakeRunner(STOPPED)
        clock = Clock()
        out = run_daily(make_cfg(), runner, lambda u: 200, clock, lambda s: clock.advance(s),
                        alas_json(d, GOOD_ALAS), log_dir)
        self.assertNotEqual(out["result"], "success")
        self.assertEqual(out["result"], "timeout")
        self.assertTrue(any(c[:2] == ["docker", "stop"] for c in runner.calls))

    def test_end_line_with_traceback_is_not_success(self):
        d = tempfile.mkdtemp()
        log_dir = Path(d)
        log_file = log_dir / "run.txt"
        log_file.write_text("old line from yesterday\n")  # existing file, appended during run
        runner = FakeRunner(STOPPED)
        clock = Clock()
        sleep = AppendOnFirstSleep(clock, log_file,
                                   ["Commission detect", "Traceback (most recent call last):", DONE])
        out = run_daily(make_cfg(), runner, lambda u: 200, clock, sleep,
                        alas_json(d, GOOD_ALAS), log_dir)
        self.assertNotEqual(out["result"], "success")
        self.assertEqual(out["result"], "alas_error")
        self.assertTrue(any(c[:2] == ["docker", "stop"] for c in runner.calls))

    def test_webui_timeout_stops_container(self):
        d = tempfile.mkdtemp()
        runner = FakeRunner(STOPPED)
        clock = Clock()
        out = run_daily(make_cfg(), runner, lambda u: 503, clock, lambda s: clock.advance(s),
                        alas_json(d, GOOD_ALAS), Path(d))
        self.assertEqual(out["result"], "webui_timeout")
        self.assertTrue(any(c[:2] == ["docker", "stop"] for c in runner.calls))

    def test_apply_happens_before_docker_start(self):
        d = tempfile.mkdtemp()
        cfg_path = alas_json(d, BAD_ALAS)
        seen = {}

        class SnapshotRunner(FakeRunner):
            def run(self, args, timeout):
                if args[:2] == ["docker", "start"]:
                    seen["at_start"] = json.loads(cfg_path.read_text())
                return super().run(args, timeout)

        clock = Clock()
        out = run_daily(make_cfg(), SnapshotRunner(STOPPED), lambda u: 200, clock,
                        lambda s: clock.advance(s), cfg_path, Path(d), apply=True)
        self.assertEqual(out["result"], "timeout")
        self.assertTrue(seen["at_start"]["GameManager"]["AutoRestart"])

    def test_bad_config_without_apply_is_config_failed(self):
        d = tempfile.mkdtemp()
        runner = FakeRunner(STOPPED)
        out = run_daily(make_cfg(), runner, lambda u: 200, Clock(), lambda s: None,
                        alas_json(d, BAD_ALAS), Path(d))
        self.assertEqual(out["result"], "config_failed")
        self.assertFalse(any(c[:2] == ["docker", "start"] for c in runner.calls))

    def test_apply_never_touches_config_while_running(self):
        d = tempfile.mkdtemp()
        cfg_path = alas_json(d, BAD_ALAS)
        out = run_daily(make_cfg(), FakeRunner({**OK, "inspect": (0, "true")}), lambda u: 200,
                        Clock(), lambda s: None, cfg_path, Path(d), apply=True)
        self.assertEqual(out["result"], "already_running")
        self.assertFalse(json.loads(cfg_path.read_text())["GameManager"]["AutoRestart"])
        self.assertFalse((Path(d) / "alas.json.bak").exists())


if __name__ == "__main__":
    unittest.main()
