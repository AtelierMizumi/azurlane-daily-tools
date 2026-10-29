import json, tempfile, unittest
from unittest import mock
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

    def _stage(self, out, name):
        return [st for st in out["stages"] if st[0] == name]

    def test_stop_failure_recorded(self):
        d = tempfile.mkdtemp()
        runner = FakeRunner({**STOPPED, "docker stop": (1, "boom")})
        clock = Clock()
        out = run_daily(make_cfg(), runner, lambda u: 200, clock, lambda s: clock.advance(s),
                        alas_json(d, GOOD_ALAS), Path(d))
        self.assertEqual(out["result"], "timeout")  # unchanged by the stop failure
        stop = self._stage(out, "docker stop")
        self.assertEqual(len(stop), 1)
        self.assertFalse(stop[0][1])
        self.assertIn("boom", stop[0][2])

    def test_stop_failure_without_output_carries_rc(self):
        d = tempfile.mkdtemp()
        runner = FakeRunner({**STOPPED, "docker stop": (1, "")})
        clock = Clock()
        out = run_daily(make_cfg(), runner, lambda u: 200, clock, lambda s: clock.advance(s),
                        alas_json(d, GOOD_ALAS), Path(d))
        stop = self._stage(out, "docker stop")
        self.assertFalse(stop[0][1])
        self.assertIn("rc=1", stop[0][2])

    def test_start_failure_without_output_carries_rc(self):
        d = tempfile.mkdtemp()
        runner = FakeRunner({**STOPPED, "docker start": (125, "")})
        out = run_daily(make_cfg(), runner, lambda u: 200, Clock(), lambda s: None,
                        alas_json(d, GOOD_ALAS), Path(d))
        self.assertEqual(out["result"], "alas_error")
        start = self._stage(out, "docker start")
        self.assertFalse(start[0][1])
        self.assertIn("rc=125", start[0][2])

    def test_stop_raise_still_returns_dict(self):
        d = tempfile.mkdtemp()

        class StopRaises(FakeRunner):
            def run(self, args, timeout):
                if args[:2] == ["docker", "stop"]:
                    self.calls.append(args)
                    raise RuntimeError("daemon gone")
                return super().run(args, timeout)

        clock = Clock()
        out = run_daily(make_cfg(), StopRaises(STOPPED), lambda u: 200, clock,
                        lambda s: clock.advance(s), alas_json(d, GOOD_ALAS), Path(d))
        self.assertIsInstance(out, dict)
        self.assertEqual(out["result"], "timeout")
        stop = self._stage(out, "docker stop")
        self.assertEqual(len(stop), 1)
        self.assertFalse(stop[0][1])
        self.assertIn("RuntimeError", stop[0][2])

    def test_snapshot_unstatable_file_does_not_crash(self):
        d = tempfile.mkdtemp()
        log_dir = Path(d)
        (log_dir / "locked.txt").write_text(f"{DONE}\n")
        real_stat = Path.stat

        def flaky_stat(self, *a, **kw):
            if self.name == "locked.txt":
                raise PermissionError("denied")
            return real_stat(self, *a, **kw)

        runner = FakeRunner(STOPPED)
        clock = Clock()
        with mock.patch.object(Path, "stat", flaky_stat):
            out = run_daily(make_cfg(), runner, lambda u: 200, clock, lambda s: clock.advance(s),
                            alas_json(d, GOOD_ALAS), log_dir)
        self.assertEqual(out["result"], "timeout")
        self.assertTrue(any(c[:2] == ["docker", "stop"] for c in runner.calls))

    def test_snapshot_vanished_file_does_not_crash(self):
        d = tempfile.mkdtemp()
        log_dir = Path(d)
        # glob lists the file, but it is gone by the time the snapshot stats it
        gone = log_dir / "gone.txt"
        with mock.patch("azurlane_daily._LogTail._list", lambda self, snapshot: [gone]):
            runner = FakeRunner(STOPPED)
            clock = Clock()
            out = run_daily(make_cfg(), runner, lambda u: 200, clock, lambda s: clock.advance(s),
                            alas_json(d, GOOD_ALAS), log_dir)
        self.assertIsInstance(out, dict)
        self.assertEqual(out["result"], "timeout")
        self.assertTrue(any(c[:2] == ["docker", "stop"] for c in runner.calls))

    def test_file_failing_is_file_at_snapshot_is_never_read_later(self):
        d = tempfile.mkdtemp()
        log_dir = Path(d)
        (log_dir / "late.txt").write_text(f"App start\n{DONE}\n")  # pre-existing content
        real_is_file = Path.is_file
        failed = []

        def flaky_is_file(self, *a, **kw):
            if self.name == "late.txt" and not failed:  # fails on the first call (snapshot) only
                failed.append(1)
                raise PermissionError("denied")
            return real_is_file(self, *a, **kw)

        runner = FakeRunner(STOPPED)
        clock = Clock()
        with mock.patch.object(Path, "is_file", flaky_is_file):
            out = run_daily(make_cfg(), runner, lambda u: 200, clock, lambda s: clock.advance(s),
                            alas_json(d, GOOD_ALAS), log_dir)
        self.assertTrue(failed)
        self.assertNotEqual(out["result"], "success")
        self.assertEqual(out["result"], "timeout")
        self.assertTrue(any(c[:2] == ["docker", "stop"] for c in runner.calls))

    def test_unlistable_dir_at_snapshot_does_not_replay_old_lines(self):
        d = tempfile.mkdtemp()
        log_dir = Path(d)
        (log_dir / "old.txt").write_text(f"{DONE}\n")  # pre-existing content
        real_glob = Path.glob
        calls = []

        def flaky_glob(self, *a, **kw):
            if self == log_dir and not calls:  # first listing (snapshot) fails
                calls.append(1)
                raise PermissionError("denied")
            return real_glob(self, *a, **kw)

        clock = Clock()
        with mock.patch.object(Path, "glob", flaky_glob):
            out = run_daily(make_cfg(), FakeRunner(STOPPED), lambda u: 200, clock,
                            lambda s: clock.advance(s), alas_json(d, GOOD_ALAS), log_dir)
        self.assertEqual(out["result"], "timeout")


class Cli(unittest.TestCase):
    def test_report_lines(self):
        from azurlane_daily import format_e2e_report
        text = format_e2e_report([("preflight", True, "ok"), ("alas", False, "timeout")])
        lines = text.splitlines()
        self.assertTrue(lines[0].startswith("PASS"))
        self.assertTrue(lines[1].startswith("FAIL"))
        self.assertIn("timeout", lines[1])

    def test_report_one_line_per_stage_with_name_and_detail(self):
        from azurlane_daily import format_e2e_report
        text = format_e2e_report([("a", True, "x"), ("b", True, "y"), ("c", False, "z")])
        lines = text.splitlines()
        self.assertEqual(len(lines), 3)
        self.assertIn("b", lines[1])
        self.assertIn("y", lines[1])

    def test_failed_docker_stop_shows_fail_with_detail(self):  # Ruling 18
        from azurlane_daily import format_e2e_report
        text = format_e2e_report([("commission", True, "done"),
                                  ("docker stop", False, "rc=1 no such container")])
        last = text.splitlines()[-1]
        self.assertTrue(last.startswith("FAIL"))
        self.assertIn("docker stop", last)
        self.assertIn("no such container", last)

    def test_exit_code_truth_table(self):  # Ruling 22
        from azurlane_daily import exit_code_for
        ok = [("docker stop", True, "ok")]
        bad_stop = [("docker stop", False, "rc=1")]
        self.assertEqual(exit_code_for({"result": "success", "stages": ok}), 0)
        self.assertEqual(exit_code_for({"result": "success", "stages": []}), 0)
        self.assertEqual(exit_code_for({"result": "success", "stages": bad_stop}), 1)
        for r in ("preflight_failed", "already_running", "config_failed",
                  "webui_timeout", "alas_error", "timeout"):
            self.assertEqual(exit_code_for({"result": r, "stages": ok}), 1, r)

    def test_parse_args_defaults(self):
        from azurlane_daily import parse_args
        ns = parse_args([])
        self.assertEqual(ns.config, "daily.yaml")
        self.assertFalse(ns.check)
        self.assertFalse(ns.apply)
        self.assertFalse(ns.e2e)

    def test_parse_args_flags(self):
        from azurlane_daily import parse_args
        ns = parse_args(["--config", "my.yaml", "--check", "--apply", "--e2e"])
        self.assertEqual(ns.config, "my.yaml")
        self.assertTrue(ns.check and ns.apply and ns.e2e)

    def _run_main(self, argv, out, runner=None):
        """Call main with all real wiring replaced; return (code, stdout, stderr, run_daily mock)."""
        import io, azurlane_daily as ad
        from contextlib import redirect_stdout, redirect_stderr
        d = tempfile.mkdtemp()
        cfg = make_cfg()
        cfg = DailyConfig(cfg.device, cfg.alas_container, cfg.alas_webui_url,
                          Path(d), cfg.run_minutes, cfg.ntfy_url)
        (Path(d) / "config").mkdir()
        alas_json(Path(d) / "config", GOOD_ALAS).rename(Path(d) / "config" / "alas.json")
        deps = (runner or FakeRunner(OK), lambda u: 200, Clock(), lambda s: None)
        so, se = io.StringIO(), io.StringIO()
        with mock.patch.object(ad, "load_daily_config", return_value=cfg), \
             mock.patch.object(ad, "_real_deps", return_value=deps), \
             mock.patch.object(ad, "run_daily", return_value=out) as rd, \
             redirect_stdout(so), redirect_stderr(se):
            code = ad.main(argv)
        return code, so.getvalue(), se.getvalue(), rd, Path(d)

    def test_main_default_runs_full_and_prints_report(self):  # Ruling 21
        out = {"result": "success", "stages": [("preflight", True, "ok")],
               "started": "s", "finished": "f"}
        code, so, se, rd, d = self._run_main([], out)
        self.assertEqual(code, 0)
        rd.assert_called_once()
        self.assertTrue(so.splitlines()[0].startswith("PASS"))
        _, kwargs = rd.call_args
        self.assertEqual(kwargs["alas_config_path"], d / "config" / "alas.json")  # Ruling 19
        self.assertEqual(kwargs["log_dir"], d / "log")
        self.assertFalse(kwargs["apply"])

    def test_main_apply_flag_passed_through(self):
        out = {"result": "success", "stages": [], "started": "s", "finished": "f"}
        _, _, _, rd, _ = self._run_main(["--apply"], out)
        self.assertTrue(rd.call_args.kwargs["apply"])

    def test_main_failed_stop_exits_1_and_warns(self):  # Ruling 18
        out = {"result": "success", "started": "s", "finished": "f",
               "stages": [("commission", True, "done"), ("docker stop", False, "rc=1")]}
        code, so, se, _, _ = self._run_main(["--e2e"], out)
        self.assertEqual(code, 1)
        self.assertIn("FAIL", so)
        self.assertIn("WARNING: ALAS container may still be running; stop it manually", se)

    def test_main_no_warning_when_stop_ok(self):
        out = {"result": "success", "started": "s", "finished": "f",
               "stages": [("docker stop", True, "ok")]}
        code, _, se, _, _ = self._run_main([], out)
        self.assertEqual(code, 0)
        self.assertNotIn("WARNING", se)

    def test_main_failure_result_exits_1(self):
        out = {"result": "timeout", "started": "s", "finished": "f",
               "stages": [("commission", False, "late"), ("docker stop", True, "ok")]}
        code, _, se, _, _ = self._run_main([], out)
        self.assertEqual(code, 1)
        self.assertNotIn("WARNING", se)

    def test_check_does_not_call_run_daily_or_start_container(self):  # Ruling 20
        runner = FakeRunner(OK)
        code, so, _, rd, _ = self._run_main(["--check"], {}, runner=runner)
        self.assertEqual(code, 0)
        rd.assert_not_called()
        self.assertFalse(any(c[:2] in (["docker", "start"], ["docker", "stop"]) for c in runner.calls))
        lines = so.splitlines()
        self.assertTrue(len(lines) >= 2)
        self.assertTrue(all(l.startswith("PASS") for l in lines))

    def test_check_fails_when_waydroid_stopped(self):  # Ruling 20
        runner = FakeRunner({**OK, "waydroid status": (0, "Session:\tSTOPPED")})
        code, so, _, rd, _ = self._run_main(["--check"], {}, runner=runner)
        self.assertEqual(code, 1)
        rd.assert_not_called()
        self.assertTrue(any(l.startswith("FAIL") for l in so.splitlines()))

    def test_check_fails_on_bad_alas_config(self):  # Ruling 20
        import azurlane_daily as ad
        out_code = None
        d = tempfile.mkdtemp()
        cfg = make_cfg()
        cfg = DailyConfig(cfg.device, cfg.alas_container, cfg.alas_webui_url,
                          Path(d), cfg.run_minutes, cfg.ntfy_url)
        (Path(d) / "config").mkdir()
        (Path(d) / "config" / "alas.json").write_text(json.dumps(BAD_ALAS))
        import io
        from contextlib import redirect_stdout
        so = io.StringIO()
        with mock.patch.object(ad, "load_daily_config", return_value=cfg), \
             mock.patch.object(ad, "_real_deps", return_value=(FakeRunner(OK), lambda u: 200, Clock(), lambda s: None)), \
             redirect_stdout(so):
            out_code = ad.main(["--check"])
        self.assertEqual(out_code, 1)
        self.assertTrue(any(l.startswith("FAIL") for l in so.getvalue().splitlines()))


if __name__ == "__main__":
    unittest.main()
