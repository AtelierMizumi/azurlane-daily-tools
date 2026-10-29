import json, tempfile, unittest
from unittest import mock
from pathlib import Path
import azurlane_daily
import preflight
from daily_config import DailyConfig, DeviceCfg
from azurlane_daily import run_daily
from test_preflight import FakeRunner, OK

ORIGINAL_RUNS_DIR = azurlane_daily.RUNS_DIR  # before setUpModule patches it

DONE = "Scheduler: End task `Commission`"

CANONICAL = ["preflight", "config", "docker start", "webui", "app started", "login ok",
             "commission started", "commission done", "summary written", "docker stop"]  # Ruling 35

GOOD_DEPLOY = """Deploy:
  Alas:
    # --run. Auto-run specified config when startup
    # '["alas"]' specified "alas" config
    Run: ["alas"]
    WebuiSSLKey: null
"""
BAD_DEPLOY = GOOD_DEPLOY.replace('Run: ["alas"]', "Run: null")

_patches = []


def setUpModule():
    # No real TCP probe of the host adb port and no writes into sidecar/runs.
    runs = tempfile.mkdtemp()
    _patches.append(mock.patch.object(preflight, "_adb_port_open", lambda *a, **k: False))
    _patches.append(mock.patch.object(azurlane_daily, "RUNS_DIR", Path(runs)))
    for p in _patches:
        p.start()


def tearDownModule():
    for p in _patches:
        p.stop()


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
                                   ["App start", "Login success", "COMMISSION SCAN", DONE])
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
        (log_dir / "run.txt").write_text(f"App start\nLogin success\nCOMMISSION SCAN\n{DONE}\n")
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
                                   ["COMMISSION SCAN", "Traceback (most recent call last):", DONE])
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
        (Path(d) / "config" / "deploy.yaml").write_text(GOOD_DEPLOY)  # Ruling 32: --check reads it
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
        (Path(d) / "config" / "deploy.yaml").write_text(GOOD_DEPLOY)
        import io
        from contextlib import redirect_stdout
        so = io.StringIO()
        with mock.patch.object(ad, "load_daily_config", return_value=cfg), \
             mock.patch.object(ad, "_real_deps", return_value=(FakeRunner(OK), lambda u: 200, Clock(), lambda s: None)), \
             redirect_stdout(so):
            out_code = ad.main(["--check"])
        self.assertEqual(out_code, 1)
        self.assertTrue(any(l.startswith("FAIL") for l in so.getvalue().splitlines()))


def make_world(alas=GOOD_ALAS, deploy=GOOD_DEPLOY):
    """A temp ALAS repo: config/alas.json, config/deploy.yaml, log/. Returns Path."""
    d = Path(tempfile.mkdtemp())
    (d / "config").mkdir()
    (d / "log").mkdir()
    (d / "config" / "alas.json").write_text(json.dumps(alas))
    if deploy is not None:
        (d / "config" / "deploy.yaml").write_text(deploy)
    return d


def go(world, runner, *, apply=False, cfg=None, http=200, runs=None, lines=None):
    clock = Clock()
    if lines is not None:
        sleep = AppendOnFirstSleep(clock, world / "log" / "run.txt", lines)
    else:
        sleep = lambda s: clock.advance(s)
    return run_daily(cfg or make_cfg(), runner, lambda u: http, clock, sleep,
                     world / "config" / "alas.json", world / "log", apply=apply,
                     deploy_path=world / "config" / "deploy.yaml",
                     runs_dir=runs or Path(tempfile.mkdtemp()))


def calls_of(runner, prefix):
    return [i for i, c in enumerate(runner.calls) if c[:len(prefix)] == prefix]


class ResolutionAfterStart(unittest.TestCase):  # Ruling 31 (C1)
    def test_no_docker_exec_before_docker_start(self):
        r = FakeRunner(STOPPED)
        out = go(make_world(), r, lines=["COMMISSION SCAN", DONE])
        start, execs = calls_of(r, ["docker", "start"]), calls_of(r, ["docker", "exec"])
        self.assertEqual(len(start), 1)
        self.assertTrue(execs)
        self.assertTrue(all(i > start[0] for i in execs))
        self.assertEqual(out["result"], "success")

    def test_connect_then_wm_size_after_start_and_before_stop(self):
        r = FakeRunner(STOPPED)
        go(make_world(), r)
        joined = [" ".join(c) for c in r.calls]
        i_start = joined.index("docker start alas")
        i_conn = joined.index("docker exec alas adb connect 192.168.240.112:5555")
        i_wm = joined.index("docker exec alas adb -s 192.168.240.112:5555 shell wm size")
        i_stop = joined.index("docker stop alas")
        self.assertLess(i_start, i_conn)
        self.assertLess(i_conn, i_wm)
        self.assertLess(i_wm, i_stop)

    def test_stopped_world_reaches_the_run(self):
        # The impossible fixture (inspect=false but exec answering) is gone: with the
        # stateful fake, a stopped container only answers exec after `docker start`.
        out = go(make_world(), FakeRunner(STOPPED))
        self.assertEqual(out["result"], "timeout")  # got past preflight and resolution

    def test_resolution_mismatch_is_preflight_failed_and_container_stopped(self):
        r = FakeRunner({**STOPPED, "wm size": (0, "Physical size: 1080x2400")})
        out = go(make_world(), r)
        self.assertEqual(out["result"], "preflight_failed")
        res = [s for s in out["stages"] if s[0] == "resolution"]
        self.assertEqual(len(res), 1)
        self.assertFalse(res[0][1])
        self.assertIn("1280x720", res[0][2])
        self.assertTrue(calls_of(r, ["docker", "stop"]))
        self.assertFalse(any(s[0] == "webui" and s[1] for s in out["stages"]))

    def test_resolution_ok_is_a_pass_stage(self):
        out = go(make_world(), FakeRunner(STOPPED))
        res = [s for s in out["stages"] if s[0] == "resolution"]
        self.assertEqual(len(res), 1)
        self.assertTrue(res[0][1])

    def test_preflight_failure_before_start_never_execs_or_starts(self):
        r = FakeRunner({**STOPPED, "waydroid status": (0, "Session:\tSTOPPED")})
        out = go(make_world(), r)
        self.assertEqual(out["result"], "preflight_failed")
        self.assertFalse(calls_of(r, ["docker", "exec"]))
        self.assertFalse(calls_of(r, ["docker", "start"]))
        self.assertFalse(calls_of(r, ["docker", "stop"]))

    def test_inspect_in_run_daily_uses_type_container(self):  # Ruling 39
        r = FakeRunner(STOPPED)
        go(make_world(), r)
        inspects = [c for c in r.calls if c[:2] == ["docker", "inspect"]]
        self.assertTrue(len(inspects) >= 2)
        for c in inspects:
            self.assertEqual(c[c.index("--type") + 1], "container")


class DeployRun(unittest.TestCase):  # Ruling 32 (C2)
    def test_bad_deploy_run_without_apply_is_config_failed(self):
        w = make_world(deploy=BAD_DEPLOY)
        r = FakeRunner(STOPPED)
        out = go(w, r)
        self.assertEqual(out["result"], "config_failed")
        self.assertFalse(calls_of(r, ["docker", "start"]))
        cfg_stage = [s for s in out["stages"] if s[0] == "config"][0]
        self.assertIn("Run", cfg_stage[2])
        self.assertEqual((w / "config" / "deploy.yaml").read_text(), BAD_DEPLOY)

    def test_apply_fixes_deploy_run_before_docker_start_with_backup(self):
        w = make_world(deploy=BAD_DEPLOY)
        seen = {}

        class SnapshotRunner(FakeRunner):
            def run(self, args, timeout):
                if args[:2] == ["docker", "start"]:
                    seen["deploy"] = (w / "config" / "deploy.yaml").read_text()
                return super().run(args, timeout)

        out = go(w, SnapshotRunner(STOPPED), apply=True)
        self.assertEqual(out["result"], "timeout")
        self.assertEqual(seen["deploy"], GOOD_DEPLOY)
        self.assertEqual((w / "config" / "deploy.yaml.bak").read_text(), BAD_DEPLOY)

    def test_apply_with_good_files_writes_no_backup(self):
        w = make_world()
        go(w, FakeRunner(STOPPED), apply=True)
        self.assertFalse((w / "config" / "deploy.yaml.bak").exists())
        self.assertFalse((w / "config" / "alas.json.bak").exists())

    def test_apply_never_touches_deploy_while_running(self):
        w = make_world(alas=BAD_ALAS, deploy=BAD_DEPLOY)
        out = go(w, FakeRunner({**OK, "inspect": (0, "true")}), apply=True)
        self.assertEqual(out["result"], "already_running")
        self.assertEqual((w / "config" / "deploy.yaml").read_text(), BAD_DEPLOY)
        self.assertFalse((w / "config" / "deploy.yaml.bak").exists())

    def test_missing_deploy_yaml_is_config_failed_not_crash(self):
        w = make_world(deploy=None)
        out = go(w, FakeRunner(STOPPED))
        self.assertEqual(out["result"], "config_failed")

    def test_check_reports_deploy_run_problem(self):
        w = make_world(deploy=BAD_DEPLOY)
        code, so = _check_main(w, FakeRunner(OK))
        self.assertEqual(code, 1)
        fail = [l for l in so.splitlines() if l.startswith("FAIL")]
        self.assertTrue(any("Run" in l for l in fail))


def _check_main(world, runner, argv=("--check",)):
    import io
    from contextlib import redirect_stdout
    cfg = make_cfg()
    cfg = DailyConfig(cfg.device, cfg.alas_container, cfg.alas_webui_url,
                      world, cfg.run_minutes, cfg.ntfy_url)
    so = io.StringIO()
    with mock.patch.object(azurlane_daily, "load_daily_config", return_value=cfg), \
         mock.patch.object(azurlane_daily, "_real_deps",
                           return_value=(runner, lambda u: 200, Clock(), lambda s: None)), \
         redirect_stdout(so):
        code = azurlane_daily.main(list(argv))
    return code, so.getvalue()


class RunningContainerAdbProbe(unittest.TestCase):  # I1
    def test_running_container_with_open_port_is_already_running(self):
        probe = mock.Mock(return_value=True)
        r = FakeRunner({**OK, "inspect": (0, "true")})
        with mock.patch.object(preflight, "_adb_port_open", probe):
            out = go(make_world(), r)
        self.assertEqual(out["result"], "already_running")
        probe.assert_not_called()
        self.assertFalse(calls_of(r, ["docker", "start"]))
        self.assertFalse(calls_of(r, ["docker", "stop"]))

    def test_check_running_container_open_port_no_host_adb_problem(self):
        probe = mock.Mock(return_value=True)
        r = FakeRunner(OK)
        with mock.patch.object(preflight, "_adb_port_open", probe):
            code, so = _check_main(make_world(), r)
        self.assertEqual(code, 0)
        self.assertNotIn("host adb", so)
        probe.assert_not_called()
        self.assertTrue(calls_of(r, ["docker", "exec"]))  # resolution still checked

    def test_stopped_container_with_open_port_still_fails_preflight(self):
        r = FakeRunner(STOPPED)
        with mock.patch.object(preflight, "_adb_port_open", lambda *a, **k: True):
            out = go(make_world(), r)
        self.assertEqual(out["result"], "preflight_failed")
        self.assertIn("host adb server", out["stages"][0][2])
        self.assertFalse(calls_of(r, ["docker", "start"]))


class MalformedAlasJson(unittest.TestCase):  # Minor 1
    def test_apply_with_non_dict_intermediate_is_config_failed(self):
        w = make_world(alas={"GameManager": {"AutoRestart": False}, "Commission": None})
        r = FakeRunner(STOPPED)
        out = go(w, r, apply=True)
        self.assertEqual(out["result"], "config_failed")
        stage = [s for s in out["stages"] if s[0] == "config"][0]
        self.assertFalse(stage[1])
        self.assertIn("Commission", stage[2])
        self.assertFalse(calls_of(r, ["docker", "start"]))


class DeployRunAddsAlas(unittest.TestCase):  # Minor 2
    def test_apply_keeps_other_instances(self):
        w = make_world(deploy=GOOD_DEPLOY.replace('Run: ["alas"]', 'Run: ["alas2"]'))
        go(w, FakeRunner(STOPPED), apply=True)
        self.assertIn('Run: ["alas2", "alas"]', (w / "config" / "deploy.yaml").read_text())


class CheckResolutionStage(unittest.TestCase):  # Ruling 31
    def test_stopped_container_reports_skipped_resolution_and_passes(self):
        w = make_world()
        r = FakeRunner({**OK, "inspect": (0, "false")})
        code, so = _check_main(w, r)
        self.assertEqual(code, 0)
        line = [l for l in so.splitlines() if "resolution" in l]
        self.assertEqual(len(line), 1)
        self.assertTrue(line[0].startswith("PASS"))
        self.assertIn("skipped (container stopped)", line[0])
        self.assertFalse(calls_of(r, ["docker", "exec"]))

    def test_running_container_gets_resolution_checked(self):
        w = make_world()
        r = FakeRunner({**OK, "wm size": (0, "Physical size: 1080x2400")})
        code, so = _check_main(w, r)
        self.assertEqual(code, 1)
        line = [l for l in so.splitlines() if "resolution" in l]
        self.assertTrue(line[0].startswith("FAIL"))
        self.assertIn("1280x720", line[0])

    def test_check_never_starts_or_stops(self):
        r = FakeRunner({**OK, "inspect": (0, "false")})
        _check_main(make_world(), r)
        self.assertFalse(calls_of(r, ["docker", "start"]))
        self.assertFalse(calls_of(r, ["docker", "stop"]))


FUTURE = "2099-01-01 00:00:00"
PAST = "2000-01-01 00:00:00"


def alas_with_next_run(value):
    d = json.loads(json.dumps(GOOD_ALAS))
    d["Commission"]["Scheduler"]["NextRun"] = value
    return d


class NextRunWarning(unittest.TestCase):  # Ruling 33
    def test_check_future_next_run_is_a_warning_stage_not_a_failure(self):
        w = make_world(alas=alas_with_next_run(FUTURE))
        code, so = _check_main(w, FakeRunner(OK))
        self.assertEqual(code, 0)
        warn = [l for l in so.splitlines() if "WARNING: NextRun" in l]
        self.assertEqual(len(warn), 1)
        self.assertTrue(warn[0].startswith("PASS"))
        self.assertIn(FUTURE, warn[0])

    def test_check_past_next_run_has_no_warning(self):
        w = make_world(alas=alas_with_next_run(PAST))
        code, so = _check_main(w, FakeRunner(OK))
        self.assertEqual(code, 0)
        self.assertNotIn("WARNING", so)

    def test_timeout_detail_mentions_future_next_run(self):
        w = make_world(alas=alas_with_next_run(FUTURE))
        out = go(w, FakeRunner(STOPPED))
        self.assertEqual(out["result"], "timeout")
        detail = [s for s in out["stages"] if s[0] == "commission"][0][2]
        self.assertIn("NextRun", detail)
        self.assertIn(FUTURE, detail)

    def test_timeout_without_future_next_run_does_not_mention_it(self):
        w = make_world(alas=alas_with_next_run(PAST))
        out = go(w, FakeRunner(STOPPED))
        detail = [s for s in out["stages"] if s[0] == "commission"][0][2]
        self.assertNotIn("NextRun", detail)


class E2eStages(unittest.TestCase):  # Ruling 35
    def _canon(self, out):
        return [s for s in out["stages"] if s[0] in CANONICAL]

    def test_success_lists_canonical_stages_in_order_all_pass(self):
        out = go(make_world(), FakeRunner(STOPPED),
                 lines=["App start", "Login success", "COMMISSION SCAN", DONE])
        canon = self._canon(out)
        self.assertEqual([s[0] for s in canon], CANONICAL)
        self.assertTrue(all(s[1] for s in canon), canon)

    def test_stages_not_reached_are_fail_not_reached(self):
        out = go(make_world(), FakeRunner(STOPPED), lines=["App start", "COMMISSION SCAN"])
        canon = {s[0]: s for s in self._canon(out)}
        self.assertEqual([s[0] for s in self._canon(out)], CANONICAL)
        self.assertTrue(canon["app started"][1])
        self.assertTrue(canon["commission started"][1])
        for name in ("login ok", "commission done"):
            self.assertFalse(canon[name][1])
            self.assertEqual(canon[name][2], "not reached")

    def test_preflight_failure_marks_everything_after_as_not_reached(self):
        r = FakeRunner({**STOPPED, "waydroid status": (0, "Session:\tSTOPPED")})
        out = go(make_world(), r)
        canon = {s[0]: s for s in self._canon(out)}
        self.assertEqual([s[0] for s in self._canon(out)], CANONICAL)
        self.assertFalse(canon["preflight"][1])
        for name in CANONICAL[1:8]:
            self.assertEqual(canon[name][:2], (name, False))
            self.assertEqual(canon[name][2], "not reached")

    def test_webui_timeout_marks_run_state_stages_not_reached(self):
        out = go(make_world(), FakeRunner(STOPPED), http=503)
        canon = {s[0]: s for s in self._canon(out)}
        self.assertFalse(canon["webui"][1])
        for name in ("app started", "login ok", "commission started", "commission done"):
            self.assertEqual(canon[name][2], "not reached")
        self.assertTrue(canon["docker stop"][1])

    def test_not_reached_docker_stop_is_not_reported_as_a_failed_stop(self):
        from azurlane_daily import exit_code_for, _stop_failed
        out = go(make_world(), FakeRunner({**OK, "inspect": (0, "true")}))
        self.assertEqual(out["result"], "already_running")
        self.assertFalse(_stop_failed(out["stages"]))

    def test_old_log_lines_do_not_set_run_state_stages(self):  # Ruling 11 still holds
        w = make_world()
        (w / "log" / "run.txt").write_text(f"App start\nLogin success\nCOMMISSION SCAN\n{DONE}\n")
        out = go(w, FakeRunner(STOPPED))
        canon = {s[0]: s for s in self._canon(out)}
        for name in ("app started", "login ok", "commission started", "commission done"):
            self.assertEqual(canon[name][2], "not reached")


class NoCommissionIsNotSuccess(unittest.TestCase):  # Ruling 46
    # The COMMISSION fragment of a real ALAS log: the game was not running, ALAS queued
    # a Restart and ended the task without ever scanning commissions.
    SCREENSHOT = ["2026-10-29 20:01:02.123 | WARNING | Game not running",
                  "2026-10-29 20:01:02.456 | INFO | Task call: Restart",
                  "2026-10-29 20:01:03.789 | INFO | " + DONE]

    def test_end_without_commission_detect_is_not_success(self):
        out = go(make_world(), FakeRunner(STOPPED), lines=self.SCREENSHOT)
        self.assertNotEqual(out["result"], "success")
        self.assertEqual(out["result"], "no_commission")
        stage = [s for s in out["stages"] if s[0] == "commission"][0]
        self.assertFalse(stage[1])
        self.assertEqual(stage[2], "Commission ended without starting its commission scan "
                                   "(game not running?)")
        canon = {s[0]: s for s in out["stages"]}
        self.assertTrue(canon["commission done"][1])
        self.assertEqual(canon["commission started"][:2], ("commission started", False))
        self.assertTrue(calls_of_stop(out))

    def test_no_commission_still_stops_container_and_exits_1(self):
        from azurlane_daily import exit_code_for
        r = FakeRunner(STOPPED)
        out = go(make_world(), r, lines=self.SCREENSHOT)
        self.assertTrue(calls_of(r, ["docker", "stop"]))
        self.assertEqual(exit_code_for(out), 1)

    def test_errors_take_precedence_over_no_commission(self):
        out = go(make_world(), FakeRunner(STOPPED),
                 lines=["GameNotRunningError: boom", DONE])
        self.assertEqual(out["result"], "alas_error")

    def test_detect_plus_end_without_errors_is_success(self):
        out = go(make_world(), FakeRunner(STOPPED), lines=["COMMISSION SCAN", DONE])
        self.assertEqual(out["result"], "success")
        stage = [s for s in out["stages"] if s[0] == "commission"][0]
        self.assertTrue(stage[1])

    def test_detect_without_end_is_still_timeout(self):
        out = go(make_world(), FakeRunner(STOPPED), lines=["COMMISSION SCAN"])
        self.assertEqual(out["result"], "timeout")


def calls_of_stop(out):
    return [s for s in out["stages"] if s[0] == "docker stop" and s[1]]


class Summary(unittest.TestCase):  # Ruling 34
    def _summary(self, runs):
        dirs = [p for p in runs.iterdir() if p.is_dir()]
        self.assertEqual(len(dirs), 1)
        self.assertRegex(dirs[0].name, r"^\d{8}-\d{6}$")
        return json.loads((dirs[0] / "summary.json").read_text(encoding="utf-8"))

    def test_summary_json_written_with_required_fields(self):
        runs = Path(tempfile.mkdtemp())
        out = go(make_world(), FakeRunner(STOPPED), runs=runs,
                 lines=["App start", "Login success", "COMMISSION SCAN", DONE])
        data = self._summary(runs)
        self.assertEqual(data["result"], "success")
        self.assertEqual(data["serial"], "192.168.240.112:5555")
        self.assertEqual(data["started"], out["started"])
        self.assertTrue(data["finished"])
        names = [s["name"] for s in data["stages"]]
        self.assertIn("docker stop", names)
        self.assertIn("commission done", names)
        for s in data["stages"]:
            self.assertEqual(set(s), {"name", "ok", "detail"})

    def test_summary_written_stage_passes_and_sits_before_docker_stop(self):
        out = go(make_world(), FakeRunner(STOPPED))
        names = [s[0] for s in out["stages"]]
        self.assertLess(names.index("summary written"), names.index("docker stop"))
        stage = [s for s in out["stages"] if s[0] == "summary written"][0]
        self.assertTrue(stage[1])

    def test_summary_written_even_when_preflight_fails(self):
        runs = Path(tempfile.mkdtemp())
        r = FakeRunner({**STOPPED, "waydroid status": (0, "Session:\tSTOPPED")})
        go(make_world(), r, runs=runs)
        self.assertEqual(self._summary(runs)["result"], "preflight_failed")

    def test_summary_write_failure_is_a_failed_stage_not_a_raise(self):
        blocked = Path(tempfile.mkdtemp()) / "runs"
        blocked.write_text("i am a file, not a directory")
        out = go(make_world(), FakeRunner(STOPPED), runs=blocked)
        stage = [s for s in out["stages"] if s[0] == "summary written"][0]
        self.assertFalse(stage[1])
        self.assertEqual(out["result"], "timeout")

    def test_default_runs_dir_is_sidecar_runs(self):
        self.assertEqual(ORIGINAL_RUNS_DIR, Path(azurlane_daily.__file__).parent / "runs")

    def test_no_ntfy_when_url_unset(self):
        with mock.patch("azurlane_daily.urllib.request.urlopen") as uo:
            out = go(make_world(), FakeRunner(STOPPED))
        uo.assert_not_called()
        self.assertFalse([s for s in out["stages"] if s[0] == "ntfy"])

    def _cfg_ntfy(self):
        c = make_cfg()
        return DailyConfig(c.device, c.alas_container, c.alas_webui_url, c.alas_repo_dir,
                           c.run_minutes, "http://ntfy.example/topic")

    def test_ntfy_post_one_line_with_timeout_10(self):
        with mock.patch("azurlane_daily.urllib.request.urlopen") as uo:
            out = go(make_world(), FakeRunner(STOPPED), cfg=self._cfg_ntfy())
        uo.assert_called_once()
        req = uo.call_args.args[0]
        self.assertEqual(req.get_method(), "POST")
        self.assertEqual(req.full_url, "http://ntfy.example/topic")
        body = req.data.decode("utf-8")
        self.assertNotIn("\n", body)
        self.assertIn(out["result"], body)
        self.assertEqual(uo.call_args.kwargs["timeout"], 10)

    def test_ntfy_oserror_recorded_as_failed_stage_never_raised(self):
        with mock.patch("azurlane_daily.urllib.request.urlopen", side_effect=OSError("no route")):
            out = go(make_world(), FakeRunner(STOPPED), cfg=self._cfg_ntfy())
        stage = [s for s in out["stages"] if s[0] == "ntfy"]
        self.assertEqual(len(stage), 1)
        self.assertFalse(stage[0][1])
        self.assertIn("no route", stage[0][2])
        self.assertEqual(out["result"], "timeout")  # result unchanged

    def test_ntfy_success_stage_passes(self):
        with mock.patch("azurlane_daily.urllib.request.urlopen"):
            out = go(make_world(), FakeRunner(STOPPED), cfg=self._cfg_ntfy())
        stage = [s for s in out["stages"] if s[0] == "ntfy"]
        self.assertTrue(stage[0][1])


class MainConfigErrors(unittest.TestCase):  # Ruling 40
    def _main(self, side_effect=None, argv=()):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        se = io.StringIO()
        with mock.patch.object(azurlane_daily, "load_daily_config", side_effect=side_effect), \
             redirect_stdout(io.StringIO()), redirect_stderr(se):
            code = azurlane_daily.main(list(argv))
        return code, se.getvalue()

    def test_yaml_error_returns_1_with_fail_line(self):
        import yaml
        code, err = self._main(yaml.YAMLError("bad yaml"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL  daily config  ", err)
        self.assertIn("bad yaml", err)

    def test_malformed_yaml_file_end_to_end(self):
        import io
        from contextlib import redirect_stdout, redirect_stderr
        p = Path(tempfile.mkdtemp()) / "daily.yaml"
        p.write_text("device: [unclosed\n")
        se = io.StringIO()
        with redirect_stdout(io.StringIO()), redirect_stderr(se):
            code = azurlane_daily.main(["--config", str(p)])
        self.assertEqual(code, 1)
        self.assertIn("FAIL  daily config", se.getvalue())

    def test_value_error_still_handled(self):
        code, err = self._main(ValueError("daily config: missing required key 'x'"))
        self.assertEqual(code, 1)
        self.assertIn("FAIL  daily config", err)

    def test_main_passes_deploy_path_to_run_daily(self):
        import io
        from contextlib import redirect_stdout
        w = make_world()
        cfg = make_cfg()
        cfg = DailyConfig(cfg.device, cfg.alas_container, cfg.alas_webui_url, w,
                          cfg.run_minutes, cfg.ntfy_url)
        out = {"result": "success", "stages": [], "started": "s", "finished": "f"}
        deps = (FakeRunner(OK), lambda u: 200, Clock(), lambda s: None)
        with mock.patch.object(azurlane_daily, "load_daily_config", return_value=cfg), \
             mock.patch.object(azurlane_daily, "_real_deps", return_value=deps), \
             mock.patch.object(azurlane_daily, "run_daily", return_value=out) as rd, \
             redirect_stdout(io.StringIO()):
            azurlane_daily.main([])
        self.assertEqual(rd.call_args.kwargs["deploy_path"], w / "config" / "deploy.yaml")


if __name__ == "__main__":
    unittest.main()
