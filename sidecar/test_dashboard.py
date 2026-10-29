import io
import json
import tempfile
import threading
import unittest
import urllib.request
from dataclasses import replace
from pathlib import Path
from unittest import mock

import dashboard
from daily_config import DailyConfig, DeviceCfg, DaemonCfg, load_daily_config


def make_cfg(tmp: Path, **daemon) -> DailyConfig:
    return DailyConfig(
        device=DeviceCfg("waydroid", "192.0.2.10:5555", (1280, 720)),
        alas_container="alas", alas_webui_url="http://127.0.0.1:22267",
        alas_repo_dir=tmp, run_minutes=60, ntfy_url=None,
        daemon=DaemonCfg(**daemon))


class FakeProc:
    def __init__(self):
        self.stdout = io.StringIO("child says hi\n")
        self._poll = None  # None = running
        self.terminated = False
        self.killed = False

    def poll(self):
        return self._poll

    def terminate(self):
        self.terminated = True
        self._poll = -15

    def kill(self):
        self.killed = True
        self._poll = -9

    def wait(self, timeout=None):
        self._poll = self._poll or 0
        return self._poll


class FakeNotifier:
    def __init__(self):
        self.events = []

    def record_failure(self, kind, detail=""):
        self.events.append(("fail", kind))

    def record_recovery(self, kind):
        self.events.append(("ok", kind))


class DockerFake:
    """Returns canned docker inspect/start/stop results and records calls."""

    def __init__(self, running="false"):
        self.running = running
        self.calls = []

    def __call__(self, cmd, timeout=dashboard.DOCKER_TIMEOUT):
        self.calls.append(cmd)
        joined = " ".join(cmd)
        if "inspect" in joined:
            return 0, self.running
        if " start " in f" {joined} " or cmd[:2] == ["docker", "start"]:
            self.running = "true"
            return 0, self.running
        if cmd[:2] == ["docker", "stop"]:
            self.running = "false"
            return 0, "alas"
        return 0, ""


def make_dashboard(tmp: Path, docker=None, cfg=None, **daemon_kw) -> dashboard.Dashboard:
    cfg = cfg or make_cfg(tmp, **daemon_kw)
    d = dashboard.Dashboard(
        cfg, spawn=lambda args: FakeProc(), version="test",
        screen_checker=mock.Mock(stalled=mock.Mock(return_value=False)),
        notifier=FakeNotifier(), config_file=tmp / "daily.yaml")
    d.docker = docker or DockerFake()
    return d


class ServerTestCase(unittest.TestCase):
    """Real HTTP server on an ephemeral port with faked system commands."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.old_runs, self.old_state = dashboard.RUNS_DIR, dashboard.STATE_FILE
        dashboard.RUNS_DIR = self.tmp / "runs"
        dashboard.STATE_FILE = self.tmp / "runs" / "latest-state.json"
        self.docker = DockerFake()
        self.daemon = make_dashboard(self.tmp, docker=self.docker)
        self.patcher = mock.patch.object(dashboard, "_run_cmd", self.docker)
        self.patcher.start()
        self.server = self.daemon.make_server("127.0.0.1", 0)
        self.port = self.server.server_address[1]
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def tearDown(self):
        self.server.shutdown()
        self.server.server_close()
        self.patcher.stop()
        dashboard.RUNS_DIR, dashboard.STATE_FILE = self.old_runs, self.old_state

    def url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def get(self, path, headers=None):
        req = urllib.request.Request(self.url(path), headers=headers or {})
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, r.read()
        except urllib.error.HTTPError as e:
            return e.code, e.read()

    def post(self, path, body, headers=None):
        h = {"Content-Type": "application/json"}
        h.update(headers or {})
        req = urllib.request.Request(self.url(path), data=json.dumps(body).encode(),
                                     headers=h, method="POST")
        try:
            with urllib.request.urlopen(req, timeout=5) as r:
                return r.status, json.loads(r.read())
        except urllib.error.HTTPError as e:
            return e.code, json.loads(e.read())


class TestStatus(ServerTestCase):
    def test_index_serves_html(self):
        status, body = self.get("/")
        self.assertEqual(status, 200)
        self.assertIn(b"azdaily", body)

    def test_status_shape_and_container_state(self):
        status, body = self.get("/api/status")
        self.assertEqual(status, 200)
        data = json.loads(body)
        for key in ("daemon", "container", "waydroid", "watchdog", "pipeline",
                    "schedule", "config"):
            self.assertIn(key, data)
        self.assertFalse(data["container"]["running"])
        self.assertEqual(data["daemon"]["listen"], "127.0.0.1:22268")

    def test_history_reads_summaries(self):
        runs = dashboard.RUNS_DIR / "20261030-060000"
        runs.mkdir(parents=True)
        (runs / "summary.json").write_text(json.dumps(
            {"result": "success", "started": "2026-10-30T06:00:00",
             "finished": "2026-10-30T06:01:00"}))
        status, body = self.get("/api/history")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(data["runs"][0]["result"], "success")
        self.assertEqual(data["runs"][0]["dir"], "20261030-060000")

    def test_logtail_reads_newest_log(self):
        logdir = self.tmp / "log"
        logdir.mkdir()
        (logdir / "2026-10-30_alas.txt").write_text(
            "\n".join(f"line {i}" for i in range(60)))
        status, body = self.get("/api/logtail")
        data = json.loads(body)
        self.assertEqual(status, 200)
        self.assertEqual(len(data["lines"]), 50)
        self.assertEqual(data["lines"][-1], "line 59")


class TestRunLifecycle(ServerTestCase):
    def test_run_spawn_lock_and_finish(self):
        status, data = self.post("/api/run", {"mode": "run"})
        self.assertEqual(status, 200)
        self.assertTrue(self.daemon.child_alive())
        status, data = self.post("/api/run", {"mode": "run"})
        self.assertEqual(status, 409)
        # child finishes -> lock released
        self.daemon._proc._poll = 0
        status, data = self.post("/api/run", {"mode": "run"})
        self.assertEqual(status, 200)

    def test_stop_terminates_and_stops_container(self):
        self.post("/api/run", {"mode": "run"})
        status, data = self.post("/api/stop", {})
        self.assertEqual(status, 200)
        proc = self.daemon._proc
        self.assertTrue(proc.terminated)
        self.assertIn(["docker", "stop", "alas"], self.docker.calls)
        state = json.loads(dashboard.STATE_FILE.read_text())
        self.assertEqual(state["result"], "stopped_by_user")

    def test_unknown_mode_rejected(self):
        status, data = self.post("/api/run", {"mode": "explode"})
        self.assertEqual(status, 409)

    def test_container_start_and_stop(self):
        status, data = self.post("/api/container", {"action": "start"})
        self.assertEqual(status, 200)
        self.assertIn(["docker", "start", "alas"], self.docker.calls)
        status, data = self.post("/api/container", {"action": "explode"})
        self.assertEqual(status, 409)


class TestSecurity(ServerTestCase):
    def setUp(self):
        super().setUp()
        self.daemon.cfg = replace(self.daemon.cfg, daemon=DaemonCfg(token="s3cret"))

    def test_token_required_and_accepted(self):
        status, _ = self.get("/api/status")
        self.assertEqual(status, 401)
        status, _ = self.get("/api/status", headers={"X-Azdaily-Token": "s3cret"})
        self.assertEqual(status, 200)
        status, _ = self.get("/api/status", headers={"Authorization": "Bearer s3cret"})
        self.assertEqual(status, 200)

    def test_cross_origin_post_rejected(self):
        # 401 (missing token) takes precedence; send the token to reach the
        # origin check.
        status, data = self.post("/api/run", {"mode": "run"},
                                 headers={"Origin": "http://evil.example",
                                          "X-Azdaily-Token": "s3cret"})
        self.assertEqual(status, 403)


class TestSupervisor(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.docker = DockerFake(running="false")
        self.daemon = make_dashboard(self.tmp, docker=self.docker)
        self.patcher = mock.patch.object(dashboard, "_run_cmd", self.docker)
        self.patcher.start()

    def tearDown(self):
        self.patcher.stop()

    def test_always_on_starts_stopped_container(self):
        self.daemon.supervise_once()
        self.assertIn(["docker", "start", "alas"], self.docker.calls)
        self.assertEqual(self.daemon._watchdog["container"], "down")
        self.assertIn("always-on auto-start", self.daemon._watchdog["last_action"])

    def test_watchdog_records_failures_and_recovers(self):
        self.daemon.supervise_once()          # container down -> failure
        self.docker.running = "true"
        self.daemon._container_cache = (0.0, False)  # bypass the 5s status cache
        self.daemon.supervise_once()          # -> recovery (webui down either way)
        kinds = [(k, kind) for k, kind in self.daemon.notifier.events]
        self.assertIn(("fail", "container-down"), kinds)
        self.assertIn(("ok", "container-down"), kinds)

    def test_run_at_fallback_runs_once_per_day(self):
        self.daemon.cfg = replace(self.daemon.cfg,
                                  daemon=DaemonCfg(always_on=False, run_at="00:00"))
        self.daemon._maybe_scheduled_run()
        first = self.daemon._proc
        self.assertIsNotNone(first)
        first._poll = 0  # finish it so a second spawn would be allowed
        self.daemon._maybe_scheduled_run()
        self.assertIs(self.daemon._proc, first)  # same day -> no second run

    def test_run_at_future_does_not_spawn(self):
        self.daemon.cfg = replace(self.daemon.cfg,
                                  daemon=DaemonCfg(always_on=False, run_at="23:59"))
        self.daemon._maybe_scheduled_run()
        self.assertIsNone(self.daemon._proc)


class TestDaemonConfig(unittest.TestCase):
    def _write(self, tmp, text):
        p = tmp / "daily.yaml"
        p.write_text(text)
        return load_daily_config(p)

    BASE = """\
device: {profile: waydroid, serial: 192.0.2.10:5555, resolution: 1280x720}
alas: {container_name: alas, webui_url: "http://x", repo_dir: /tmp}
notify: {ntfy_url: null}
"""

    def test_defaults_when_section_missing(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._write(Path(d), self.BASE)
            self.assertEqual(cfg.daemon.listen, "127.0.0.1:22268")
            self.assertTrue(cfg.daemon.always_on)
            self.assertTrue(cfg.daemon.watchdog)
            self.assertIsNone(cfg.daemon.token)

    def test_overrides_and_listen_forms(self):
        with tempfile.TemporaryDirectory() as d:
            cfg = self._write(Path(d), self.BASE + (
                "daemon: {listen: ':9999', token: abc, always_on: false, "
                "run_at: '06:30', watchdog_interval: 120}\n"))
            self.assertEqual(cfg.daemon.listen, "127.0.0.1:9999")
            self.assertEqual(cfg.daemon.token, "abc")
            self.assertFalse(cfg.daemon.always_on)
            self.assertEqual(cfg.daemon.run_at, "06:30")
            self.assertEqual(cfg.daemon.watchdog_interval, 120)

    def test_invalid_run_at_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "run_at"):
                self._write(Path(d), self.BASE + "daemon: {run_at: '6h'}\n")

    def test_invalid_port_rejected(self):
        with tempfile.TemporaryDirectory() as d:
            with self.assertRaisesRegex(ValueError, "invalid port"):
                self._write(Path(d), self.BASE + "daemon: {listen: 'localhost:0'}\n")


if __name__ == "__main__":
    unittest.main()
